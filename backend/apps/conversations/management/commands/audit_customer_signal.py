"""Audit the CUSTOMER's raw text at each first-occurrence of a
CUSTOMER_SIGNAL event, classified via a per-signal taxonomy.

Used by Pipeline 1B-6 Phase 0 to verify whether the extractor's
CUSTOMER_HESITATION label actually corresponds to hesitation in the
raw customer text — or something else (reassurance, deferment,
extractor mislabel).

Usage:
    python manage.py audit_customer_signal \\
        --analysis-run <uuid> \\
        --condition CUSTOMER_HESITATION \\
        [--include-holdout] \\
        [--model gpt-4o-mini]

Cost: ~$0.0001 per observation. For ~25 CUSTOMER_HESITATION cases the
total is under a cent.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.analysis.conditional import Event
from apps.conversations.analysis.customer_signal_audit import (
    AUDITOR_VERSION, CUSTOMER_SIGNAL_TAXONOMIES, RawTurnText, audit,
    build_llm_classifier,
)
from apps.conversations.models import (
    ConditionalAnalysisRun, ConversationSemanticEvent, ConversationTurn,
    LearningCorpusMember,
)
from apps.learning.services.llm_client import LearningLLMClient


class Command(BaseCommand):
    help = ('Audit customer-turn text at each CUSTOMER_SIGNAL first '
            'occurrence and classify via per-signal taxonomy.')

    def add_arguments(self, parser):
        parser.add_argument('--analysis-run', required=True)
        parser.add_argument('--condition', required=True,
                            help=f'CUSTOMER_SIGNAL event type with a '
                                 f'registered taxonomy. Currently: '
                                 f'{sorted(CUSTOMER_SIGNAL_TAXONOMIES.keys())}')
        parser.add_argument('--include-holdout', action='store_true')
        parser.add_argument('--model', default='gpt-4o-mini')

    def handle(self, *args, **options):
        try:
            run = ConditionalAnalysisRun.objects.get(pk=options['analysis_run'])
        except ConditionalAnalysisRun.DoesNotExist as exc:
            raise CommandError(f'analysis_run not found: {options["analysis_run"]}') from exc

        condition = options['condition']
        include_holdout = options['include_holdout']

        conv_ids = list(run.discovery_conversation_ids or [])
        if include_holdout:
            conv_ids += list(run.holdout_conversation_ids or [])
        if not conv_ids:
            raise CommandError('analysis run has no stored conversation ids')

        self.stdout.write(self.style.NOTICE(
            f'Auditor {AUDITOR_VERSION} run={run.pk} condition={condition} '
            f'conversations={len(conv_ids)} include_holdout={include_holdout} '
            f'model={options["model"]}'
        ))

        # Load events
        events_by_conv: dict[str, list[Event]] = defaultdict(list)
        for row in (ConversationSemanticEvent.objects
                    .filter(extraction_run=run.extraction_run,
                            conversation_id__in=conv_ids)
                    .order_by('conversation_id', 'ordinal')
                    .values('conversation_id', 'event_type',
                            'turn_start', 'ordinal')):
            events_by_conv[str(row['conversation_id'])].append(Event(
                event_type=row['event_type'],
                turn_start=row['turn_start'],
                ordinal=row['ordinal'],
            ))

        # Load turn text with ordinal by occurred_at
        turns_by_conv: dict[str, list[RawTurnText]] = defaultdict(list)
        turn_rows = list(
            ConversationTurn.objects
            .filter(conversation_id__in=conv_ids)
            .order_by('conversation_id', 'occurred_at')
            .values('conversation_id', 'speaker', 'text', 'occurred_at')
        )
        grouped: dict[str, list[dict]] = defaultdict(list)
        for r in turn_rows:
            grouped[str(r['conversation_id'])].append(r)
        for conv_id, rows in grouped.items():
            for i, r in enumerate(rows):
                turns_by_conv[conv_id].append(RawTurnText(
                    ordinal=i,
                    speaker=r['speaker'] or 'unknown',
                    text=(r['text'] or '').strip(),
                ))

        # Outcomes
        pos_set = set(run.positive_class_statuses or [])
        neg_set = set(run.negative_class_statuses or [])
        outcomes: dict[str, str] = {}
        for m in (LearningCorpusMember.objects
                  .filter(corpus=run.corpus, conversation_id__in=conv_ids)
                  .values('conversation_id', 'lb_status_at_freeze')):
            status = m['lb_status_at_freeze']
            if status in pos_set:
                outcomes[str(m['conversation_id'])] = 'positive'
            elif status in neg_set:
                outcomes[str(m['conversation_id'])] = 'negative'

        try:
            classifier = build_llm_classifier(
                LearningLLMClient(), condition, model=options['model'],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        result = audit(
            conversation_events=events_by_conv,
            conversation_turns=turns_by_conv,
            conversation_outcomes=outcomes,
            condition_event=condition,
            classify_fn=classifier,
        )

        # ---------------- report ----------------
        entries = result.entries
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Classified {len(entries)} first-occurrence observations '
            f'of {condition}'
        ))
        overall_pos = sum(1 for e in entries if e.outcome_class == 'positive')
        if entries:
            self.stdout.write(
                f'Baseline positive rate: {overall_pos}/{len(entries)} '
                f'= {overall_pos/len(entries):.2f}'
            )

        rates = result.outcome_rates_by_category()
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Per-category counts + outcome rates:'
        ))
        # Ordered by count desc
        for cat in sorted(rates, key=lambda c: -rates[c][1]):
            pos, total, rate = rates[cat]
            neg = total - pos
            self.stdout.write(
                f'  {cat:32}  n={total:3}  pos={pos:3}  neg={neg:3}  '
                f'positive_rate={rate:.2f}'
            )

        # Sample entries per category
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Sample entries per category (up to 3):'
        ))
        for cat in sorted(rates, key=lambda c: -rates[c][1]):
            entries_c = [e for e in entries if e.llm_category == cat]
            if not entries_c:
                continue
            self.stdout.write(f'\n  [{cat}] {len(entries_c)} total:')
            for e in entries_c[:3]:
                snippet = e.customer_text[:120].replace('\n', ' ')
                self.stdout.write(
                    f'    conv={e.conversation_id[:8]} outcome={e.outcome_class} '
                    f'conf={e.llm_confidence:.2f}'
                )
                self.stdout.write(f'      rationale: {e.llm_rationale}')
                self.stdout.write(f'      customer: "{snippet}"')
