"""Pipeline 1B-4A: audit NO_ACTION observations from a 1B-3 conditional
analysis run.

Usage:
    python manage.py audit_no_action \\
        --analysis-run <uuid> \\
        [--condition PROPERTY_DETAILS_PROVIDED --condition AVAILABILITY_REQUESTED]

Splits every NO_ACTION observation for the requested conditions into
finer-grained causes (TRUE_NO_RESPONSE, EXTRACTION_MISS,
SYSTEM_AUTOMATION_RESPONSE, OUTCOME_PROXY_TRUNCATED_WINDOW,
CUSTOMER_IMMEDIATELY_SENT_NEXT_SIGNAL, AGENT_REPLIED_OUTSIDE_RESPONSE_WINDOW,
CONVERSATION_ENDED_BEFORE_REPLY, OTHER) and reports per-reason
conversation counts + outcome rates. No new DB models — derived
analytical state per the "derived states in analyzer, not ontology"
principle.

Defaults to auditing PROPERTY_DETAILS_PROVIDED + AVAILABILITY_REQUESTED
(the two conditions whose NO_ACTION rates 1B-4 flagged as >40%).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.analysis.conditional import Event
from apps.conversations.analysis.no_action_audit import (
    AUDITOR_VERSION, RawTurn, audit,
)
from apps.conversations.models import (
    ConditionalAnalysisRun, Conversation, ConversationSemanticEvent,
    ConversationTurn, LearningCorpusMember, OutcomeSnapshot,
)


DEFAULT_CONDITIONS = ('PROPERTY_DETAILS_PROVIDED', 'AVAILABILITY_REQUESTED')


class Command(BaseCommand):
    help = 'Classify NO_ACTION observations from a conditional analysis run.'

    def add_arguments(self, parser):
        parser.add_argument('--analysis-run', required=True,
                            help='ConditionalAnalysisRun UUID to audit')
        parser.add_argument('--condition', action='append', default=[],
                            help='Repeatable. Default: '
                                 'PROPERTY_DETAILS_PROVIDED, AVAILABILITY_REQUESTED')
        parser.add_argument('--include-holdout', action='store_true',
                            help='Also audit holdout-set conversations. '
                                 'Default: discovery only.')

    def handle(self, *args, **options):
        try:
            run = ConditionalAnalysisRun.objects.get(pk=options['analysis_run'])
        except ConditionalAnalysisRun.DoesNotExist as exc:
            raise CommandError(f'analysis_run not found: {options["analysis_run"]}') from exc

        conditions = tuple(options['condition']) or DEFAULT_CONDITIONS
        include_holdout = options['include_holdout']

        # Build the conversation set from the same split the analyzer used.
        conv_ids = list(run.discovery_conversation_ids or [])
        if include_holdout:
            conv_ids += list(run.holdout_conversation_ids or [])
        if not conv_ids:
            raise CommandError('analysis run has no stored conversation ids')

        self.stdout.write(self.style.NOTICE(
            f'Auditor {AUDITOR_VERSION} run={run.pk} '
            f'conditions={list(conditions)} '
            f'conversations={len(conv_ids)} '
            f'include_holdout={include_holdout}'
        ))

        # --- Bulk load: events, turns, outcomes for these conversations ---
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

        # Load turns and assign ordinal by occurred_at within each conv.
        turns_by_conv: dict[str, list[RawTurn]] = defaultdict(list)
        turn_rows = list(
            ConversationTurn.objects
            .filter(conversation_id__in=conv_ids)
            .order_by('conversation_id', 'occurred_at')
            .values('conversation_id', 'speaker', 'occurred_at')
        )
        # Group + assign ordinals in-order
        grouped: dict[str, list[dict]] = defaultdict(list)
        for r in turn_rows:
            grouped[str(r['conversation_id'])].append(r)
        for conv_id, rows in grouped.items():
            for i, r in enumerate(rows):
                turns_by_conv[conv_id].append(RawTurn(
                    ordinal=i, speaker=r['speaker'] or 'unknown',
                ))

        # Outcomes from corpus membership (lb_status_at_freeze) — same
        # binary classification the 1B-3 analyzer used.
        pos_set = set(run.positive_class_statuses or [])
        neg_set = set(run.negative_class_statuses or [])
        outcomes: dict[str, str] = {}
        for m in (LearningCorpusMember.objects
                  .filter(corpus=run.corpus,
                          conversation_id__in=conv_ids)
                  .values('conversation_id', 'lb_status_at_freeze')):
            status = m['lb_status_at_freeze']
            if status in pos_set:
                outcomes[str(m['conversation_id'])] = 'positive'
            elif status in neg_set:
                outcomes[str(m['conversation_id'])] = 'negative'

        # --- Run audit ---
        max_turn_distance = int(
            (run.config or {}).get('max_turn_distance', 20)
        )
        result = audit(
            conversation_events=events_by_conv,
            conversation_turns=turns_by_conv,
            conversation_outcomes=outcomes,
            conditions=conditions,
            max_turn_distance=max_turn_distance,
        )

        # --- Report ---
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Total NO_ACTION observations: {len(result.entries)}'
        ))
        self.stdout.write('')

        grouped_by_c = defaultdict(list)
        for e in result.entries:
            grouped_by_c[e.condition_event].append(e)

        for cond in conditions:
            entries = grouped_by_c.get(cond, [])
            if not entries:
                self.stdout.write(self.style.WARNING(
                    f'{cond}: 0 NO_ACTION observations in this run'
                ))
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'\n=== {cond}: {len(entries)} NO_ACTION observations ==='
            ))
            reason_counts = Counter(e.reason_fine for e in entries)
            for reason, n in reason_counts.most_common():
                slice_ = [e for e in entries if e.reason_fine == reason]
                pos = sum(1 for e in slice_ if e.outcome_class == 'positive')
                neg = sum(1 for e in slice_ if e.outcome_class == 'negative')
                total = pos + neg
                rate = pos / total if total else 0.0
                self.stdout.write(
                    f'  {reason:44}  n={n:4}  '
                    f'pos={pos:3}  neg={neg:3}  '
                    f'positive_rate={rate:.2f}'
                )
            # Baseline: outcome rate across ALL entries for this condition
            all_pos = sum(1 for e in entries if e.outcome_class == 'positive')
            all_neg = sum(1 for e in entries if e.outcome_class == 'negative')
            all_total = all_pos + all_neg
            if all_total:
                self.stdout.write(
                    f'  {"OVERALL NO_ACTION for this condition":44}  '
                    f'n={all_total:4}  pos={all_pos:3}  neg={all_neg:3}  '
                    f'positive_rate={all_pos/all_total:.2f}'
                )

        # Interpretation guide
        self.stdout.write('')
        self.stdout.write(self.style.NOTICE('Interpretation:'))
        self.stdout.write(
            '  TRUE_NO_RESPONSE, CONVERSATION_ENDED_BEFORE_REPLY  '
            '= real agent silence (business problem if outcome low)'
        )
        self.stdout.write(
            '  EXTRACTION_MISS                                    '
            '= extractor limitation (revisit prompt/ontology)'
        )
        self.stdout.write(
            '  SYSTEM_AUTOMATION_RESPONSE                         '
            '= automation replied but no AGENT_ACTION was emitted'
        )
        self.stdout.write(
            '  OUTCOME_PROXY_TRUNCATED_WINDOW                     '
            '= outcome already decided; window closed for legitimate reasons'
        )
        self.stdout.write(
            '  CUSTOMER_IMMEDIATELY_SENT_NEXT_SIGNAL              '
            '= customer supplied more info in the next turn — analyzer '
            'correctly waited for the settled state (usually not a problem)'
        )
        self.stdout.write(
            '  AGENT_REPLIED_OUTSIDE_RESPONSE_WINDOW              '
            '= agent DID reply but slower than the window allowed — worth '
            'checking whether widening max_turn_distance would help'
        )
