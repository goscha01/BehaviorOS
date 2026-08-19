"""Pipeline 1B-3: conditional (customer signal → agent action) analysis.

Usage:
    python manage.py analyze_conditional \\
        --org <uuid> \\
        --corpus spotless_lb_quo@v1 \\
        --extractor-model gpt-4o-mini \\
        [--split-seed 42] [--min-cell-support 8] [--max-turn-distance 20] \\
        [--positive-status booked --positive-status completed ...] \\
        [--negative-status lost ...]

For each customer-signal type C observed in the corpus and each agent
action A observed as the first response to C, computes:
  - rate(positive | C+A)  vs  rate(positive | C + other AGENT_ACTION)  (primary)
  - rate(positive | C+A)  vs  rate(positive | C + no response)          (secondary)
with holdout replication and length-stratified direction check.

Persists to ConditionalAnalysisRun + ConditionalActionPattern.
Prints a compact ranked report at the end (SUPPORTED first).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.conversations.analysis.conditional import (
    ANALYZER_VERSION, ConditionalConfig, analyze, build_records,
)
from apps.conversations.models import (
    ConditionalActionPattern, ConditionalAnalysisRun, LearningCorpus,
    SemanticExtractionRun,
)
from apps.conversations.semantic.extractor import EXTRACTOR_VERSION
from apps.conversations.semantic.ontology import ONTOLOGY_VERSION
from apps.conversations.semantic.prompt import PROMPT_VERSION


class Command(BaseCommand):
    help = 'Pipeline 1B-3 — conditional customer-signal → agent-action analysis.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True)
        parser.add_argument('--corpus', required=True,
                            help='Format: name@version')
        parser.add_argument('--extractor-model', required=True,
                            help='Model of the extraction run to analyze (e.g. gpt-4o-mini)')
        parser.add_argument('--split-seed', type=int, default=42,
                            help='Same seed = same 80/20 split as 1B-2. Default 42.')
        parser.add_argument('--min-cell-support', type=int, default=8,
                            help='Minimum conversations in the C+A cell to mark SUPPORTED')
        parser.add_argument('--max-turn-distance', type=int, default=20,
                            help='Safety bound on response window (event-based '
                                 'terminators usually close sooner)')
        parser.add_argument('--positive-status', action='append', default=[],
                            help='Repeatable. Default: booked, in_progress, completed')
        parser.add_argument('--negative-status', action='append', default=[],
                            help='Repeatable. Default: lost')

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

        pos = tuple(options['positive_status']) or ConditionalConfig().positive_statuses
        neg = tuple(options['negative_status']) or ConditionalConfig().negative_statuses
        config = ConditionalConfig(
            positive_statuses=pos, negative_statuses=neg,
            min_cell_support=options['min_cell_support'],
            max_turn_distance=options['max_turn_distance'],
        )

        self.stdout.write(self.style.NOTICE(
            f'Conditional analysis: corpus={name}@{version} '
            f'extraction={extraction_run.pk} seed={options["split_seed"]} '
            f'positive={pos} negative={neg}'
        ))

        records, load_meta = build_records(corpus, extraction_run, config=config)
        self.stdout.write(
            f'  records included: {load_meta["n_included"]}  '
            f'(excluded: lead_mismatch={load_meta["n_excluded_lead_mismatch"]} '
            f'status_outside_binary={load_meta["n_excluded_status"]})'
        )
        if len(records) < 20:
            raise CommandError(
                f'insufficient records for conditional analysis: n={len(records)}. '
                f'Need at least 20 with binary outcomes.'
            )

        results, meta = analyze(records, config=config,
                                split_seed=options['split_seed'])
        supported = sum(1 for r in results if r.overall_status == 'SUPPORTED')
        reproduced = sum(1 for r in results if r.holdout_status == 'HOLDOUT_REPRODUCED')

        with transaction.atomic():
            run = ConditionalAnalysisRun.objects.create(
                org=org, corpus=corpus, extraction_run=extraction_run,
                analyzer_version=ANALYZER_VERSION,
                split_seed=options['split_seed'],
                config=config.as_dict(),
                status=ConditionalAnalysisRun.Status.RUNNING,
                started_at=timezone.now(),
                positive_class_statuses=list(pos),
                negative_class_statuses=list(neg),
                n_excluded_lead_mismatch=load_meta['n_excluded_lead_mismatch'],
                n_excluded_status=load_meta['n_excluded_status'],
                **{k: meta[k] for k in (
                    'n_discovery_positive', 'n_discovery_negative',
                    'n_holdout_positive', 'n_holdout_negative',
                    'n_discovery_observations', 'n_holdout_observations',
                    'discovery_conversation_ids', 'holdout_conversation_ids',
                )},
            )
            for i, r in enumerate(results, 1):
                ConditionalActionPattern.objects.create(
                    analysis_run=run, pattern_id=f'CA{i:04d}',
                    condition_event=r.condition_event,
                    action_event=r.action_event,
                    d_ca_positive=r.d_ca_pos, d_ca_negative=r.d_ca_neg,
                    d_co_positive=r.d_co_pos, d_co_negative=r.d_co_neg,
                    d_cn_positive=r.d_cn_pos, d_cn_negative=r.d_cn_neg,
                    d_ca_rate=r.d_ca_rate, d_co_rate=r.d_co_rate,
                    d_cn_rate=r.d_cn_rate,
                    d_primary_effect=r.d_primary_effect,
                    d_primary_ci_low=r.d_primary_ci_low,
                    d_primary_ci_high=r.d_primary_ci_high,
                    d_secondary_effect=r.d_secondary_effect,
                    d_secondary_ci_low=r.d_secondary_ci_low,
                    d_secondary_ci_high=r.d_secondary_ci_high,
                    length_adjusted_direction_short=r.len_short_dir,
                    length_adjusted_direction_long=r.len_long_dir,
                    h_ca_positive=r.h_ca_pos, h_ca_negative=r.h_ca_neg,
                    h_co_positive=r.h_co_pos, h_co_negative=r.h_co_neg,
                    h_cn_positive=r.h_cn_pos, h_cn_negative=r.h_cn_neg,
                    h_primary_effect=r.h_primary_effect,
                    h_secondary_effect=r.h_secondary_effect,
                    overall_status=r.overall_status,
                    holdout_status=r.holdout_status,
                    evidence_positive_ids=r.evidence_positive_ids,
                    evidence_negative_ids=r.evidence_negative_ids,
                )
            run.patterns_found = len(results)
            run.patterns_supported = supported
            run.patterns_reproduced_on_holdout = reproduced
            run.status = ConditionalAnalysisRun.Status.COMPLETED
            run.completed_at = timezone.now()
            run.save()

        self._print_report(run, results)

    def _print_report(self, run: ConditionalAnalysisRun,
                      results: list) -> None:
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Conditional analysis run {run.pk} — status={run.status}'
        ))
        self.stdout.write(
            f'  splits:   discovery pos={run.n_discovery_positive} '
            f'neg={run.n_discovery_negative}  '
            f'holdout pos={run.n_holdout_positive} neg={run.n_holdout_negative}'
        )
        self.stdout.write(
            f'  observations: discovery={run.n_discovery_observations} '
            f'holdout={run.n_holdout_observations}'
        )
        self.stdout.write(
            f'  excluded: lead_mismatch={run.n_excluded_lead_mismatch} '
            f'status_outside_binary={run.n_excluded_status}'
        )
        self.stdout.write(
            f'  results:  {run.patterns_found} cells '
            f'({run.patterns_supported} SUPPORTED, '
            f'{run.patterns_reproduced_on_holdout} reproduced on holdout)'
        )
        self.stdout.write('')
        # Only print SUPPORTED cells in the top report — the whole point
        # of 1B-3 is "10 credible actionable comparisons > 127 generic".
        supported = [r for r in results if r.overall_status == 'SUPPORTED']
        supported.sort(key=lambda r: -abs(r.d_primary_effect))
        if not supported:
            self.stdout.write(self.style.WARNING(
                '  no SUPPORTED cells at current min_cell_support. '
                'Lower --min-cell-support to inspect DIRECTIONAL_ONLY cells.'
            ))
            return
        self.stdout.write('SUPPORTED conditional patterns (ranked by |primary effect|):')
        self.stdout.write('')
        for i, r in enumerate(supported, 1):
            pid = f'CA{results.index(r) + 1:04d}'
            sign = '+' if r.d_primary_effect >= 0 else ''
            self.stdout.write(
                f'  {pid}  Given {r.condition_event}:'
            )
            self.stdout.write(
                f'    action = {r.action_event}  '
                f'CA rate={r.d_ca_rate:.2f} ({r.d_ca_pos}/{r.d_ca_pos + r.d_ca_neg})  '
                f'vs other-A rate={r.d_co_rate:.2f} ({r.d_co_pos}/{r.d_co_pos + r.d_co_neg})'
            )
            self.stdout.write(
                f'    primary effect: {sign}{r.d_primary_effect:.2f}  '
                f'CI[{r.d_primary_ci_low:+.2f}, {r.d_primary_ci_high:+.2f}]'
            )
            self.stdout.write(
                f'    secondary vs no-response: {r.d_secondary_effect:+.2f}  '
                f'(no-response n={r.d_cn_pos + r.d_cn_neg})'
            )
            self.stdout.write(
                f'    length: short={r.len_short_dir or "-":8}  '
                f'long={r.len_long_dir or "-":8}  '
                f'holdout={r.holdout_status}'
            )
            self.stdout.write('')
        underpowered = sum(1 for r in results if r.overall_status == 'UNDERPOWERED')
        directional = sum(1 for r in results if r.overall_status == 'DIRECTIONAL_ONLY')
        if underpowered or directional:
            self.stdout.write(
                f'  (also stored: {directional} DIRECTIONAL_ONLY, '
                f'{underpowered} UNDERPOWERED — see ConditionalActionPattern rows)'
            )
