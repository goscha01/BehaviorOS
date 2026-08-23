"""Quality Manager V1 HTTP surface.

Endpoints:
  POST /api/v1/quality-manager/run?tenantId=<uuid>
      Queue a QM run against the tenant's latest completed
      reconstruction. Idempotent per qm_version. Returns run_id.

  GET  /api/v1/quality-manager/runs/<run_id>
      Poll status + stats.

  GET  /api/v1/quality-manager/findings?tenantId=<uuid>[&dimension=X][&state=FAIL][&limit=N]
      Tenant-scoped list of FAIL findings + corpus-level patterns
      by default. Filterable.

  GET  /api/v1/quality-manager/findings/<eval_id>
      Full detail for one QualityEvaluation.

  GET  /api/v1/quality-manager/conversation/<conv_id>
      Owner-facing "quality report card" for one conversation —
      every dimension's result + evidence for that conversation.
"""

from __future__ import annotations

from rest_framework import status as http_status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.conversations.api.auth import InsightsServiceTokenAuthentication


class QualityManagerRunView(APIView):
    """POST /api/v1/quality-manager/run?tenantId=<uuid>[&qmVersion=X]"""

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.conversations.models import (
            UnifiedBusinessReconstructionRun as _URun,
        )
        from apps.quality_manager.engine import create_or_reuse_run
        from apps.quality_manager.tasks import run_quality_manager_task

        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        qm_version = (
            request.query_params.get('qmVersion') or 'qm-v1'
        ).strip()

        recon = (
            _URun.objects
            .filter(tenant_external_id=tenant, status='completed')
            .order_by('-created_at').first()
        )
        if recon is None:
            raise NotFound({
                'detail': f'no completed reconstruction for tenant {tenant}',
            })
        run, created = create_or_reuse_run(
            recon, qm_version=qm_version, dimensions=None,
        )
        if run.status == 'completed':
            return Response({
                'run_id': str(run.id),
                'status': 'completed',
                'created': False,
                'note': 'existing completed run reused',
                'stats': run.stats_json,
                'poll_url': f'/api/v1/quality-manager/runs/{run.id}',
            })
        async_result = run_quality_manager_task.delay(
            reconstruction_run_id=str(recon.id),
            qm_version=qm_version,
        )
        return Response({
            'run_id': str(run.id),
            'task_id': async_result.id,
            'status': run.status,
            'created': created,
            'dimensions_enabled': run.dimensions_enabled_json,
            'poll_url': f'/api/v1/quality-manager/runs/{run.id}',
        }, status=http_status.HTTP_202_ACCEPTED)


class QualityManagerRunStatusView(APIView):
    """GET /api/v1/quality-manager/runs/<uuid>"""

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, run_id):
        from apps.quality_manager.models import QualityRun

        try:
            run = QualityRun.objects.get(pk=run_id)
        except QualityRun.DoesNotExist:
            raise NotFound({'detail': f'no run {run_id}'})
        return Response({
            'run_id': str(run.id),
            'org_id': str(run.org_id),
            'reconstruction_run_id': str(run.reconstruction_run_id),
            'qm_version': run.qm_version,
            'dimensions_enabled': run.dimensions_enabled_json,
            'status': run.status,
            'started_at': (
                run.started_at.isoformat() if run.started_at else None
            ),
            'completed_at': (
                run.completed_at.isoformat() if run.completed_at else None
            ),
            'conversations_evaluated': run.conversations_evaluated,
            'stats': run.stats_json,
            'error_message': run.error_message,
        })


class QualityManagerFindingsListView(APIView):
    """GET /api/v1/quality-manager/findings?tenantId=<uuid>&state=FAIL"""

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.conversations.models import (
            UnifiedBusinessReconstructionRun as _URun,
        )
        from apps.quality_manager.models import (
            QualityEvaluation, QualityRun,
        )

        tenant = (request.query_params.get('tenantId') or '').strip()
        if not tenant:
            raise ValidationError({'tenantId': 'required'})
        recon = (
            _URun.objects
            .filter(tenant_external_id=tenant, status='completed')
            .order_by('-created_at').first()
        )
        if recon is None:
            raise NotFound({
                'detail': f'no completed reconstruction for tenant {tenant}',
            })
        run = (
            QualityRun.objects
            .filter(org=recon.org, reconstruction_run=recon)
            .order_by('-created_at').first()
        )
        if run is None:
            raise NotFound({
                'detail': (
                    f'no QM run for tenant {tenant} — call '
                    '/api/v1/quality-manager/run first'
                ),
            })

        qs = QualityEvaluation.objects.filter(run=run)
        state = (request.query_params.get('state') or '').strip()
        if state:
            qs = qs.filter(state=state)
        else:
            qs = qs.filter(state='FAIL')  # default: findings only
        dimension = (request.query_params.get('dimension') or '').strip()
        if dimension:
            qs = qs.filter(dimension=dimension)
        try:
            limit = min(
                int(request.query_params.get('limit') or 200), 500,
            )
        except ValueError:
            limit = 200

        rows = list(qs[:limit])
        return Response({
            'tenant_external_id': tenant,
            'run_id': str(run.id),
            'run_status': run.status,
            'total_matched': qs.count(),
            'returned': len(rows),
            'evaluations': [_serialize_eval(r) for r in rows],
        })


class QualityManagerFindingDetailView(APIView):
    """GET /api/v1/quality-manager/findings/<uuid>"""

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, evaluation_id):
        from apps.quality_manager.models import QualityEvaluation

        try:
            ev = QualityEvaluation.objects.get(pk=evaluation_id)
        except QualityEvaluation.DoesNotExist:
            raise NotFound({'detail': f'no evaluation {evaluation_id}'})
        return Response(_serialize_eval(ev, include_run=True))


class QualityManagerConversationReportView(APIView):
    """GET /api/v1/quality-manager/conversation/<uuid>

    Owner-facing "quality report card" for one conversation.
    Returns every dimension's evaluation for this conversation
    from the latest QualityRun that touched it.
    """

    authentication_classes = [InsightsServiceTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, conversation_id):
        from apps.conversations.models import Conversation as _Conv
        from apps.quality_manager.models import QualityEvaluation

        try:
            conv = _Conv.objects.get(pk=conversation_id)
        except _Conv.DoesNotExist:
            raise NotFound({
                'detail': f'no conversation {conversation_id}',
            })
        latest_run = (
            QualityEvaluation.objects
            .filter(conversation=conv)
            .order_by('-run__created_at').values_list('run_id', flat=True)
            .first()
        )
        if latest_run is None:
            raise NotFound({
                'detail': (
                    f'no QM evaluations for conversation {conversation_id}'
                ),
            })
        evals = list(
            QualityEvaluation.objects
            .filter(conversation=conv, run_id=latest_run)
            .order_by('dimension', 'created_at')
        )
        # Aggregate per dimension for the report card.
        by_dim: dict[str, dict] = {}
        for ev in evals:
            d = by_dim.setdefault(ev.dimension, {
                'dimension': ev.dimension,
                'evaluations': [],
                'counts': {
                    'PASS': 0, 'FAIL': 0,
                    'UNKNOWN_NOT_EVALUABLE': 0, 'NOT_APPLICABLE': 0,
                },
            })
            d['evaluations'].append(_serialize_eval(ev))
            d['counts'][ev.state] = d['counts'].get(ev.state, 0) + 1

        return Response({
            'conversation_id': str(conv.id),
            'org_id': str(conv.org_id),
            'source': conv.source,
            'source_conversation_id': conv.source_conversation_id,
            'channel': conv.channel,
            'started_at': (
                conv.started_at.isoformat() if conv.started_at else None
            ),
            'run_id': str(latest_run),
            'dimensions': list(by_dim.values()),
        })


def _serialize_eval(ev, include_run: bool = False) -> dict:
    out = {
        'evaluation_id': str(ev.id),
        'conversation_id': (
            str(ev.conversation_id) if ev.conversation_id else None
        ),
        'dimension': ev.dimension,
        'state': ev.state,
        'severity': ev.severity or None,
        'reason_code': ev.reason_code,
        'rationale_text': ev.rationale_text,
        'subject_key': ev.subject_key_json,
        'evidence': ev.evidence_json,
        'source_reconstructed_fact_id': (
            str(ev.source_reconstructed_fact_id)
            if ev.source_reconstructed_fact_id else None
        ),
        'created_at': ev.created_at.isoformat(),
    }
    if include_run:
        out['run_id'] = str(ev.run_id)
    return out
