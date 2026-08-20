"""Pipeline 1B-4B: audit the semantic content of the actual agent reply
after a specific customer signal — cross-referenced against what the
extractor labeled the event as.

Usage:
    python manage.py audit_action_semantics \\
        --analysis-run <uuid> \\
        --condition SERVICE_DETAILS_PROVIDED \\
        [--include-holdout] \\
        [--model gpt-4o-mini] \\
        [--limit N]

Prints per-category breakdown and outcome rates, plus a cross-tab of
extractor-labeled action vs LLM semantic category so you can see where
the extractor is missing or mislabeling.

Cost budget: ~$0.001 per observation. For ~50 SERVICE_DETAILS_PROVIDED
observations, total cost is around $0.05.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.analysis.action_semantics_audit import (
    AUDITOR_VERSION, TurnText, audit, build_llm_classifier,
)
from apps.conversations.analysis.conditional import Event
from apps.conversations.models import (
    ConditionalAnalysisRun, ConversationSemanticEvent, ConversationTurn,
    LearningCorpusMember,
)
from apps.learning.services.llm_client import LearningLLMClient


class Command(BaseCommand):
    help = ('Pipeline 1B-4B: cross-check extractor-labeled actions '
            'against LLM classification of the raw agent reply text.')

    def add_arguments(self, parser):
        parser.add_argument('--analysis-run', required=True)
        parser.add_argument('--condition', required=True,
                            help='One CUSTOMER_SIGNAL event type, e.g. '
                                 'SERVICE_DETAILS_PROVIDED')
        parser.add_argument('--include-holdout', action='store_true')
        parser.add_argument('--model', default='gpt-4o-mini',
                            help='LLM model for reply classification')
        parser.add_argument('--taxonomy', default='generic',
                            help='Classification taxonomy: "generic" (default '
                                 'six categories) or a condition-specific name '
                                 'like "price_requested" (nine categories '
                                 'covering price + explanation + discount + '
                                 'scope/value + qualification + '
                                 'booking-instead-of-price)')
        parser.add_argument('--limit', type=int, default=0,
                            help='Optional cap on # observations classified '
                                 '(0 = no cap)')

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

        # Load turns with text, ordinal by occurred_at
        turns_by_conv: dict[str, list[TurnText]] = defaultdict(list)
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
                turns_by_conv[conv_id].append(TurnText(
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

        # Optional cap by hard-truncating conversations before running
        if options['limit'] and options['limit'] > 0:
            capped = dict(list(events_by_conv.items())[:options['limit']])
            events_by_conv = capped

        max_turn_distance = int(
            (run.config or {}).get('max_turn_distance', 20)
        )
        try:
            classifier = build_llm_classifier(
                LearningLLMClient(),
                model=options['model'],
                taxonomy=options['taxonomy'],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        result = audit(
            conversation_events=events_by_conv,
            conversation_turns=turns_by_conv,
            conversation_outcomes=outcomes,
            condition_event=condition,
            classify_fn=classifier,
            max_turn_distance=max_turn_distance,
        )

        # ---------- report ----------
        entries = result.entries
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Classified {len(entries)} first-occurrence observations '
            f'of {condition}'
        ))
        overall_pos = sum(1 for e in entries if e.outcome_class == 'positive')
        overall_total = len(entries)
        if overall_total:
            self.stdout.write(
                f'Baseline positive rate over all observations: '
                f'{overall_pos}/{overall_total} = {overall_pos/overall_total:.2f}'
            )
        self.stdout.write('')

        # LLM category breakdown with outcome rates
        self.stdout.write(self.style.MIGRATE_HEADING(
            'LLM semantic category × outcome:'
        ))
        rates = result.outcome_rates_by_category()
        for cat in sorted(rates, key=lambda c: -rates[c][1]):
            pos, total, rate = rates[cat]
            neg = total - pos
            self.stdout.write(
                f'  {cat:30}  n={total:3}  pos={pos:3}  neg={neg:3}  '
                f'positive_rate={rate:.2f}'
            )

        # Cross-tab: extracted action × LLM category
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Extracted action (rows) × LLM category (columns):'
        ))
        # Pull the taxonomy's own category order so the cross-tab
        # matches the chosen taxonomy (generic vs price_requested vs
        # future condition-specific ones).
        from apps.conversations.analysis.action_semantics_audit import (
            CONDITION_TAXONOMIES, SEMANTIC_CATEGORIES,
        )
        if options['taxonomy'] == 'generic':
            cats_ordered = ['substantive_next_step', 'generic_follow_up',
                            'acknowledgment_only', 'customer_continues_details',
                            'true_no_response', 'mixed_or_unclear']
        else:
            cats_ordered = sorted(
                CONDITION_TAXONOMIES[options['taxonomy']]['categories']
            )
        # Row = extracted action, Col = LLM category
        matrix: dict[str, Counter] = defaultdict(Counter)
        for e in entries:
            matrix[e.extracted_action][e.llm_category] += 1
        header = 'extracted_action'.ljust(28) + ' | ' + ' | '.join(
            c[:14].rjust(14) for c in cats_ordered
        )
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        for action in sorted(matrix, key=lambda a: -sum(matrix[a].values())):
            row = matrix[action]
            cells = ' | '.join(str(row.get(c, 0)).rjust(14) for c in cats_ordered)
            self.stdout.write(f'{action[:28].ljust(28)} | {cells}')

        # Sample rows to eyeball. Split by LLM category and print up to 3 each.
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('Sample entries per LLM category:'))
        for cat in cats_ordered:
            entries_c = [e for e in entries if e.llm_category == cat]
            if not entries_c:
                continue
            self.stdout.write(f'\n  [{cat}] {len(entries_c)} total; showing up to 3:')
            for e in entries_c[:3]:
                snippet = e.agent_reply_text[:120].replace('\n', ' ')
                fine = f' fine={e.no_action_fine_reason}' if e.no_action_fine_reason else ''
                self.stdout.write(
                    f'    conv={e.conversation_id[:8]} '
                    f'outcome={e.outcome_class} '
                    f'extracted={e.extracted_action}{fine} '
                    f'conf={e.llm_confidence:.2f}'
                )
                self.stdout.write(f'      rationale: {e.llm_rationale}')
                if snippet:
                    self.stdout.write(f'      reply: "{snippet}"')
                else:
                    self.stdout.write(f'      reply: (empty)')

        # Ranked outcome-rate table for the chosen taxonomy
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'=== Positive rate by LLM category ({options["taxonomy"]}) ==='
        ))
        for cat in cats_ordered:
            if cat in rates:
                pos, total, rate = rates[cat]
                self.stdout.write(
                    f'  after {condition} → {cat:36} '
                    f'n={total:3} positive_rate={rate:.2f}'
                )
