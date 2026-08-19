"""Run semantic event extraction against a frozen LearningCorpus.

Usage:
    # 30-record eval
    python manage.py extract_semantic_events \\
        --corpus spotless_lb_quo@v1 \\
        --org <uuid> \\
        --limit 30

    # Full corpus
    python manage.py extract_semantic_events \\
        --corpus spotless_lb_quo@v1 \\
        --org <uuid>

Idempotent per (extraction_run, conversation). Reruns skip already-
extracted conversations. Bump extractor/ontology/prompt version in
code to force fresh extraction under a new run.
"""

from __future__ import annotations

from collections import Counter
from statistics import mean, median

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Organization
from apps.conversations.models import (
    ConversationSemanticEvent, LearningCorpus, LearningCorpusMember,
)
from apps.conversations.semantic.extractor import (
    EXTRACTOR_VERSION, ExtractRecordResult, run_extraction,
)
from apps.conversations.semantic.ontology import ONTOLOGY_VERSION
from apps.conversations.semantic.prompt import PROMPT_VERSION


class Command(BaseCommand):
    help = 'Run Pipeline 1B-1 semantic event extraction on a corpus.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True)
        parser.add_argument('--corpus', required=True,
                            help='Format: name@version, e.g. spotless_lb_quo@v1')
        parser.add_argument('--model', default=None,
                            help='Override SEMANTIC_EXTRACTION_MODEL setting')
        parser.add_argument('--limit', type=int, default=None,
                            help='Cap on conversations processed (eval subset)')
        parser.add_argument('--sample-status', action='append', default=[],
                            help='Sample only members whose lb_status_at_freeze matches '
                                 '(repeatable). Used for balanced eval subsets.')
        parser.add_argument('--max-tokens', type=int, default=4000,
                            help='Per-LLM-call output token budget')
        parser.add_argument('--conv-ids', default='',
                            help='Comma-separated conversation UUIDs to restrict to')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(pk=options['org'])
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {options["org"]} not found') from exc

        try:
            name, version = options['corpus'].split('@', 1)
        except ValueError as exc:
            raise CommandError('--corpus must be name@version') from exc
        try:
            corpus = LearningCorpus.objects.get(
                org=org, name=name, version=version,
            )
        except LearningCorpus.DoesNotExist as exc:
            raise CommandError(f'Corpus {name}@{version} not found for org={org.id}') from exc

        model = options['model'] or getattr(settings, 'SEMANTIC_EXTRACTION_MODEL', 'gpt-4o-mini')

        # Build the conversation-id subset per --sample-status /
        # --limit / --conv-ids.
        member_qs = LearningCorpusMember.objects.filter(corpus=corpus)
        if options['sample_status']:
            member_qs = member_qs.filter(
                lb_status_at_freeze__in=options['sample_status'],
            )
        if options['conv_ids']:
            ids = [x.strip() for x in options['conv_ids'].split(',') if x.strip()]
            member_qs = member_qs.filter(conversation_id__in=ids)

        # Deterministic ordering for reproducibility.
        member_qs = member_qs.order_by('created_at', 'conversation_id')
        if options['limit']:
            member_qs = member_qs[:options['limit']]

        conv_ids = list(member_qs.values_list('conversation_id', flat=True))

        self.stdout.write(self.style.NOTICE(
            f'Extraction — corpus={name}@{version} '
            f'extractor={EXTRACTOR_VERSION} ontology={ONTOLOGY_VERSION} '
            f'prompt={PROMPT_VERSION} model={model} '
            f'conversations={len(conv_ids)}'
        ))

        idx = [0]
        def _on_record(rec: ExtractRecordResult):
            idx[0] += 1
            marker = '.' if rec.ok else ('E' if rec.error else 'S')
            self.stdout.write(
                f'  [{idx[0]:>4}] {marker} conv={rec.conversation_id[:8]} '
                f'events={rec.events_created} rej={rec.events_rejected} '
                f'chunks={rec.chunks} cost=${rec.cost_usd}',
                ending='\n',
            )

        outcome = run_extraction(
            corpus, org=org, model=model,
            conversation_ids=conv_ids, max_tokens=options['max_tokens'],
            on_record=_on_record,
        )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Extraction complete.'))
        self.stdout.write(f'  records_processed:    {outcome.records_processed}')
        self.stdout.write(f'  records_skipped:      {outcome.records_skipped}')
        self.stdout.write(f'  records_failed:       {outcome.records_failed}')
        self.stdout.write(f'  events_created:       {outcome.events_created}')
        self.stdout.write(f'  events_rejected:      {outcome.events_rejected}')
        self.stdout.write(f'  input_tokens:         {outcome.input_tokens}')
        self.stdout.write(f'  output_tokens:        {outcome.output_tokens}')
        self.stdout.write(f'  cost_usd:             ${outcome.cost_usd}')

        # Post-run distributions.
        run_events = ConversationSemanticEvent.objects.filter(
            extraction_run__corpus=corpus,
            extraction_run__extractor_version=EXTRACTOR_VERSION,
            extraction_run__ontology_version=ONTOLOGY_VERSION,
            extraction_run__prompt_version=PROMPT_VERSION,
            extraction_run__model=model,
        )
        by_type = Counter(run_events.values_list('event_type', flat=True))
        by_actor = Counter(run_events.values_list('actor', flat=True))
        per_conv = [
            run_events.filter(conversation_id=c).count() for c in conv_ids
        ]

        self.stdout.write('')
        self.stdout.write('Event-type distribution:')
        for t, n in by_type.most_common():
            self.stdout.write(f'  {t:32} {n}')

        self.stdout.write('')
        self.stdout.write('Actor distribution:')
        for a, n in by_actor.most_common():
            self.stdout.write(f'  {a:16} {n}')

        if per_conv:
            self.stdout.write('')
            self.stdout.write(
                f'Events/conversation:  n={len(per_conv)} '
                f'mean={mean(per_conv):.1f} median={median(per_conv):.0f} '
                f'min={min(per_conv)} max={max(per_conv)}'
            )
