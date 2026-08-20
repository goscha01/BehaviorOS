"""Run the Pipeline 1D pricing extractor over a corpus.

Usage:
    python manage.py extract_observed_pricing --corpus <uuid>
    python manage.py extract_observed_pricing --corpus <uuid> --limit 20
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.models import LearningCorpus
from apps.conversations.observed_config.pricing.extractor import (
    run_extraction,
)
from apps.learning.services.llm_client import LearningLLMClient


class Command(BaseCommand):
    help = 'Run the observed-config pricing extractor over a corpus.'

    def add_arguments(self, parser):
        parser.add_argument('--corpus', required=True,
                             help='LearningCorpus UUID')
        parser.add_argument('--limit', type=int, default=None,
                             help='Max conversations to process (smoke test)')
        parser.add_argument('--model', default='gpt-4o-mini')

    def handle(self, *args, **options):
        try:
            corpus = LearningCorpus.objects.get(pk=options['corpus'])
        except LearningCorpus.DoesNotExist as exc:
            raise CommandError(
                f'LearningCorpus {options["corpus"]} not found'
            ) from exc
        self.stdout.write(
            f'Running pricing extractor on corpus {corpus.pk} '
            f'({corpus.name} v{corpus.version}); '
            f'limit={options.get("limit")!r}'
        )
        run = run_extraction(
            org=corpus.org,
            corpus=corpus,
            llm_client=LearningLLMClient(),
            model=options['model'],
            limit=options.get('limit'),
        )
        self.stdout.write(self.style.SUCCESS(
            f'Done. run_id={run.id} status={run.status} '
            f'processed={run.conversations_processed} '
            f'facts={run.facts_emitted} '
            f'reviews={run.ontology_review_candidates_emitted} '
            f'cost=${run.llm_cost_usd}'
        ))
