"""Run the Pipeline 1D LB pricing config parser over a snapshot.

Usage:
    python manage.py parse_configured_pricing --snapshot <uuid>
    python manage.py parse_configured_pricing --tenant <lb-user-uuid>
      (uses the latest snapshot for that tenant)
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.models import TenantConfigSnapshot
from apps.conversations.observed_config.pricing.config_parser import (
    parse_snapshot,
)
from apps.learning.services.llm_client import LearningLLMClient


class Command(BaseCommand):
    help = 'Run the observed-config pricing parser over a snapshot.'

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', default=None,
                             help='TenantConfigSnapshot UUID')
        parser.add_argument('--tenant', default=None,
                             help='LB userId (tenant_external_id); '
                                   'picks latest snapshot for tenant')
        parser.add_argument('--model', default='gpt-4o-mini')

    def handle(self, *args, **options):
        snap = None
        if options.get('snapshot'):
            try:
                snap = TenantConfigSnapshot.objects.get(pk=options['snapshot'])
            except TenantConfigSnapshot.DoesNotExist as exc:
                raise CommandError(
                    f'snapshot {options["snapshot"]} not found'
                ) from exc
        elif options.get('tenant'):
            snap = (
                TenantConfigSnapshot.objects
                .filter(source_system='leadbridge',
                         tenant_external_id=options['tenant'])
                .order_by('-created_at').first()
            )
            if snap is None:
                raise CommandError(
                    f'no snapshot for tenant {options["tenant"]}'
                )
        else:
            raise CommandError('pass --snapshot or --tenant')

        self.stdout.write(
            f'Running pricing config parser on snapshot={snap.pk} '
            f'(tenant={snap.tenant_external_id}, '
            f'sg={snap.service_group or "-"})'
        )
        run = parse_snapshot(
            snapshot=snap,
            llm_client=LearningLLMClient(),
            model=options['model'],
        )
        self.stdout.write(self.style.SUCCESS(
            f'Done. run_id={run.id} status={run.status} '
            f'facts={run.facts_emitted} cost=${run.llm_cost_usd}'
        ))
