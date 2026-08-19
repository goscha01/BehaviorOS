"""Pipeline 1B-2: analyze semantic events for candidate behavioral patterns.

Usage:
    python manage.py analyze_patterns \\
        --org <uuid> \\
        --corpus spotless_lb_quo@v1 \\
        --extractor-model gpt-4o-mini \\
        [--split-seed 42] [--min-support 5] \\
        [--positive-status booked --positive-status completed ...] \\
        [--negative-status lost ...] \\
        [--sequence-size 2 --sequence-size 3]

Runs stratified 80/20 discovery/holdout split, enumerates single-event
+ N-gram patterns from PRE_OUTCOME events, length-stratified sensitivity
check, holdout replication. Persists everything to
PatternDiscoveryRun + CandidatePattern.

Prints a compact ranked report at the end.
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.analysis.discovery import (
    ANALYZER_VERSION, DiscoveryConfig, build_records, discover,
)
from apps.conversations.models import (
    CandidatePattern, LearningCorpus, PatternDiscoveryRun,
    SemanticExtractionRun,
)
from apps.conversations.semantic.extractor import EXTRACTOR_VERSION
from apps.conversations.semantic.ontology import ONTOLOGY_VERSION
from apps.conversations.semantic.prompt import PROMPT_VERSION


class Command(BaseCommand):
    help = 'Pipeline 1B-2 — discover candidate behavioral patterns.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True)
        parser.add_argument('--corpus', required=True,
                            help='Format: name@version')
        parser.add_argument('--extractor-model', required=True,
                            help='Model of the extraction run to analyze (e.g. gpt-4o-mini)')
        parser.add_argument('--split-seed', type=int, default=42)
        parser.add_argument('--min-support', type=int, default=5,
                            help='Minimum discovery-set support (positive+negative) '
                                 'for a pattern to be reported')
        parser.add_argument('--positive-status', action='append', default=[],
                            help='Repeatable. Default: booked, in_progress, completed')
        parser.add_argument('--negative-status', action='append', default=[],
                            help='Repeatable. Default: lost')
        parser.add_argument('--sequence-size', action='append', default=[],
                            type=int, help='Repeatable. Default: 2, 3')

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
            corpus = LearningCorpus.objects.get(org=org, name=name, version=version)
        except LearningCorpus.DoesNotExist as exc:
            raise CommandError(f'Corpus {name}@{version} not found') from exc

        try:
            extraction_run = SemanticExtractionRun.objects.get(
                corpus=corpus,
                extractor_version=EXTRACTOR_VERSION,
                ontology_version=ONTOLOGY_VERSION,
                prompt_version=PROMPT_VERSION,
                model=options['extractor_model'],
            )
        except SemanticExtractionRun.DoesNotExist as exc:
            raise CommandError(
                f'No extraction run found for corpus={name}@{version} '
                f'extractor={EXTRACTOR_VERSION} ontology={ONTOLOGY_VERSION} '
                f'prompt={PROMPT_VERSION} model={options["extractor_model"]}'
            ) from exc

        # Config
        pos = tuple(options['positive_status']) or DiscoveryConfig().positive_statuses
        neg = tuple(options['negative_status']) or DiscoveryConfig().negative_statuses
        seqs = tuple(options['sequence_size']) or DiscoveryConfig().sequence_sizes
        config = DiscoveryConfig(
            positive_statuses=pos, negative_statuses=neg,
            sequence_sizes=seqs, min_support=options['min_support'],
        )

        self.stdout.write(self.style.NOTICE(
            f'Discovery run: corpus={name}@{version} extraction={extraction_run.pk} '
            f'seed={options["split_seed"]} positive={pos} negative={neg}'
        ))

        # Build records + run discovery
        records = build_records(corpus, extraction_run, config=config)
        outcome_dist = Counter(r.outcome_class for r in records)
        self.stdout.write(
            f'  records eligible for binary analysis: {len(records)} '
            f'(positive={outcome_dist["positive"]} negative={outcome_dist["negative"]})'
        )
        if outcome_dist['positive'] < 5 or outcome_dist['negative'] < 5:
            raise CommandError(
                f'insufficient records for binary analysis: {dict(outcome_dist)}. '
                f'Need at least 5 per class.'
            )

        candidates, meta = discover(records, config=config,
                                     split_seed=options['split_seed'])

        with transaction.atomic():
            run = PatternDiscoveryRun.objects.create(
                org=org, corpus=corpus, extraction_run=extraction_run,
                analyzer_version=ANALYZER_VERSION,
                split_seed=options['split_seed'],
                config=config.as_dict(),
                status=PatternDiscoveryRun.Status.RUNNING,
                started_at=timezone.now(),
                positive_class_statuses=list(pos),
                negative_class_statuses=list(neg),
                **{k: meta[k] for k in (
                    'n_discovery_positive', 'n_discovery_negative',
                    'n_holdout_positive', 'n_holdout_negative',
                    'discovery_conversation_ids', 'holdout_conversation_ids',
                )},
            )
            reproduced = 0
            for i, c in enumerate(candidates, 1):
                pid = f'P{i:04d}'
                CandidatePattern.objects.create(
                    discovery_run=run, pattern_id=pid,
                    kind=c.kind, sequence=c.sequence, temporal_class=c.temporal_class,
                    discovery_positive_present=c.d_pos_pres,
                    discovery_positive_total=c.d_pos_total,
                    discovery_negative_present=c.d_neg_pres,
                    discovery_negative_total=c.d_neg_total,
                    discovery_positive_rate=c.d_pos_rate,
                    discovery_negative_rate=c.d_neg_rate,
                    discovery_effect_size=c.d_effect,
                    discovery_effect_ci_low=c.d_ci_low,
                    discovery_effect_ci_high=c.d_ci_high,
                    length_adjusted_direction_short=c.len_short_dir,
                    length_adjusted_direction_long=c.len_long_dir,
                    holdout_positive_present=c.h_pos_pres,
                    holdout_positive_total=c.h_pos_total,
                    holdout_negative_present=c.h_neg_pres,
                    holdout_negative_total=c.h_neg_total,
                    holdout_positive_rate=c.h_pos_rate,
                    holdout_negative_rate=c.h_neg_rate,
                    holdout_effect_size=c.h_effect,
                    holdout_status=c.h_status,
                    evidence_positive_ids=c.evidence_positive_ids,
                    evidence_negative_ids=c.evidence_negative_ids,
                )
                if c.h_status == 'reproduced':
                    reproduced += 1
            run.candidates_found = len(candidates)
            run.candidates_reproduced_on_holdout = reproduced
            run.status = PatternDiscoveryRun.Status.COMPLETED
            run.completed_at = timezone.now()
            run.save()

        self._print_report(run, candidates)

    def _print_report(self, run: PatternDiscoveryRun, candidates: list) -> None:
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Discovery run {run.pk} — status={run.status}'
        ))
        self.stdout.write(
            f'  splits:   discovery pos={run.n_discovery_positive} neg={run.n_discovery_negative}  '
            f'holdout pos={run.n_holdout_positive} neg={run.n_holdout_negative}'
        )
        self.stdout.write(
            f'  results:  {run.candidates_found} candidates, '
            f'{run.candidates_reproduced_on_holdout} reproduced on holdout'
        )
        self.stdout.write('')
        self.stdout.write('Top candidates (by |effect size|, discovery set):')
        self.stdout.write('')
        for i, c in enumerate(candidates[:25], 1):
            pid = f'P{i:04d}'
            # ASCII arrow (`->`) for cross-console portability; Windows
            # cp1252 chokes on `→`.
            seq = ' -> '.join(c.sequence) if c.kind == 'ordered_sequence' else c.sequence[0]
            sign = '+' if c.d_effect >= 0 else ''
            self.stdout.write(
                f'  {pid} [{c.temporal_class:13}] {c.kind:16} '
                f'discovery pos={c.d_pos_rate:.2f} neg={c.d_neg_rate:.2f} '
                f'effect={sign}{c.d_effect:.2f} '
                f'CI[{c.d_ci_low:+.2f},{c.d_ci_high:+.2f}]  '
                f'holdout={c.h_status:15} '
                f'length: short={c.len_short_dir or "-":8} long={c.len_long_dir or "-":8}'
            )
            self.stdout.write(f'         sequence: {seq}')
        if len(candidates) > 25:
            self.stdout.write(f'  ... {len(candidates) - 25} more (see CandidatePattern rows)')
