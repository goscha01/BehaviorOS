"""Pipeline 1B-4: run the LLM normalizer against a TenantConfigSnapshot
and persist the extracted BehavioralPolicy rows.

Usage:
    python manage.py normalize_config_snapshot \\
        --snapshot <snapshot-uuid> \\
        [--model gpt-4o-mini]

Idempotent per (snapshot, condition, actions, channel) — reruns don't
duplicate rows. Rerunning with a different model creates fresh policies
under the same snapshot (the differentiator is model_used, tracked via
BehavioralPolicy.source_pointer['normalizer_model']).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.analysis.config_normalizer import (
    NORMALIZER_VERSION, normalize,
)
from apps.conversations.models import BehavioralPolicy, TenantConfigSnapshot


class Command(BaseCommand):
    help = 'Extract BehavioralPolicy rows from a TenantConfigSnapshot.'

    def add_arguments(self, parser):
        parser.add_argument('--snapshot', required=True,
                            help='TenantConfigSnapshot UUID')
        parser.add_argument('--model', default='gpt-4o-mini',
                            help='LLM model for extraction. Default gpt-4o-mini.')

    def handle(self, *args, **options):
        try:
            snapshot = TenantConfigSnapshot.objects.get(pk=options['snapshot'])
        except TenantConfigSnapshot.DoesNotExist as exc:
            raise CommandError(f'snapshot {options["snapshot"]} not found') from exc

        self.stdout.write(self.style.NOTICE(
            f'Normalizing snapshot={snapshot.pk} '
            f'source={snapshot.source_system} '
            f'tenant={snapshot.tenant_external_id} '
            f'sg={snapshot.service_group or "-"} '
            f'model={options["model"]}'
        ))

        result = normalize(snapshot.raw_config, model=options['model'])
        self.stdout.write(
            f'  extracted={len(result.policies)} '
            f'rejected={len(result.rejected)} '
            f'llm_tokens=in{result.llm_input_tokens}/out{result.llm_output_tokens} '
            f'cost=${result.llm_cost_usd}'
        )

        # Idempotent upsert by (snapshot, condition, tuple(actions), channel).
        # BehavioralPolicy has no unique constraint (multiple sources can
        # produce same-shape rules), but we dedup here to avoid rerun bloat.
        existing = {
            (p.condition_event, tuple(p.prescribed_action_events or []), p.channel):
                p
            for p in BehavioralPolicy.objects.filter(snapshot=snapshot)
        }
        inserted = 0
        for i, ep in enumerate(result.policies):
            key = (ep.condition_event, tuple(ep.prescribed_action_events), ep.channel)
            if key in existing:
                continue
            pointer = dict(ep.source_pointer or {})
            pointer.setdefault('normalizer_version', NORMALIZER_VERSION)
            pointer.setdefault('normalizer_model', result.model_used)
            BehavioralPolicy.objects.create(
                snapshot=snapshot,
                condition_event=ep.condition_event,
                prescribed_action_events=ep.prescribed_action_events,
                channel=ep.channel,
                source_rule_text=ep.source_rule_text,
                source_pointer=pointer,
                extraction_confidence=ep.extraction_confidence,
                ordinal=len(existing) + i,
            )
            inserted += 1

        self.stdout.write(self.style.SUCCESS(
            f'inserted={inserted} new policies '
            f'(skipped {len(result.policies) - inserted} duplicates already in snapshot)'
        ))
        for r in result.rejected[:10]:
            self.stdout.write(f'  rejected: {r["reason"]}')
        if len(result.rejected) > 10:
            self.stdout.write(f'  ... {len(result.rejected) - 10} more rejected')
