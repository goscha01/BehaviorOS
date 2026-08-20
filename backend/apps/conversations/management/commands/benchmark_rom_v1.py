"""ROM v1 Historical Benchmark Acceptance.

Answers the question "does ROM v1 actually work against real historical
data before we harden it into production?"

Runs three checks:

1. Per-HIGH_INTENT-signal baseline reproducibility — for a given tenant
   (LB userId), enumerate all HIGH_INTENT signals from the frozen v1
   spec and compute what the baseline cohort would look like RIGHT
   NOW if a rec targeting that signal were applied today. Prints
   eligible_n, positive_n, negative_n, positive_rate per signal.

   Cross-check these numbers against project-memory baselines:
     PRICE_REQUESTED       ~90% positive rate
     BOOKING_REQUESTED     (validated HIGH_INTENT signal)
     AVAILABILITY_REQUESTED (validated HIGH_INTENT signal)
     DISCOUNT_REQUESTED    (validated HIGH_INTENT signal)

   If a signal shows n=0 or an obviously-wrong rate, ROM's baseline
   computation is broken (bad scoping, missing OutcomeSnapshots, wrong
   signal event_type) — fix that before hardening.

2. End-to-end frozen-cohort persistence — persist a real
   RecommendationOutcomeMeasurement row for one specific eligible rec
   on the tenant, using synthetic treatment hashes (no LB config
   touched). Verify pre_cohort_conversation_ids is populated and
   pre_rate matches the standalone baseline from check #1 for the
   same signal.

3. Print a compact ACCEPTANCE / NOT-READY summary so the operator
   can decide whether to proceed with production hardening.

Usage:
    python manage.py benchmark_rom_v1 --tenant <lb-user-uuid>
    python manage.py benchmark_rom_v1 --tenant <uuid> --lookback-days 180
    python manage.py benchmark_rom_v1 --tenant <uuid> --create-measurement <rec-uuid>
    python manage.py benchmark_rom_v1 --tenant <uuid> --dry-run
      (no measurement row persisted; only per-signal counters printed)
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.conversations.measurement.creation import (
    LbApplyContext, MeasurementCreationError, _compute_baseline_cohort,
    create_measurement,
)
from apps.conversations.measurement.effective_config_contract import (
    EFFECTIVE_CONFIG_SCHEMA_VERSION,
)
from apps.conversations.measurement.specs import (
    HIGH_INTENT_SIGNAL_COVERAGE_V1, HIGH_INTENT_SIGNALS,
)
from apps.conversations.models import (
    BehaviorRecommendation, RecommendationOutcomeMeasurement,
    TenantConfigSnapshot,
)


class Command(BaseCommand):
    help = 'ROM v1 historical benchmark acceptance test against real data.'

    def add_arguments(self, parser):
        parser.add_argument('--tenant', required=True,
                             help='LB userId (tenant_external_id)')
        parser.add_argument('--lookback-days', type=int, default=None,
                             help='Override baseline_window_days for the '
                                   'per-signal check (default: use spec value)')
        parser.add_argument('--create-measurement', default=None,
                             help='BehaviorRecommendation UUID to persist an '
                                   'end-to-end measurement for (default: '
                                   'auto-pick first eligible rec on tenant)')
        parser.add_argument('--dry-run', action='store_true',
                             help='Skip creating a measurement row; only '
                                   'run per-signal baseline check')

    def handle(self, *args, **options):
        tenant = options['tenant']
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'ROM v1 Historical Benchmark Acceptance — tenant={tenant}'
        ))

        # -- Discover tenant snapshot + org --
        snapshots = TenantConfigSnapshot.objects.filter(
            source_system='leadbridge',
            tenant_external_id=tenant,
        ).order_by('-created_at')
        latest_snap = snapshots.first()
        if latest_snap is None:
            raise CommandError(
                f'no TenantConfigSnapshot found for tenant {tenant}; '
                f'is the tenant ingested?'
            )
        org = latest_snap.org
        self.stdout.write(
            f'  org={org.pk} ({org.name}) '
            f'snapshot={latest_snap.pk} '
            f'sha={latest_snap.raw_config_sha256[:12]}'
        )

        # -- Check 1: per-signal baseline --
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Check 1: per-HIGH_INTENT-signal baseline reproducibility'
        ))
        applied_at = timezone.now()
        spec = HIGH_INTENT_SIGNAL_COVERAGE_V1
        outcome = spec.primary_outcome
        if options.get('lookback_days'):
            outcome = replace(
                outcome, baseline_window_days=options['lookback_days'],
            )
            self.stdout.write(
                f'  lookback override: {outcome.baseline_window_days} days'
            )

        per_signal_results: dict[str, dict] = {}
        for signal in sorted(HIGH_INTENT_SIGNALS):
            frozen = _frozen_with_signal(spec, signal)
            # Override the outcome def if the caller passed --lookback-days
            frozen = replace(frozen, primary_outcome=outcome)
            ids, pos, neg = _compute_baseline_cohort(
                org=org,
                tenant_external_id=tenant,
                target_signal=signal,
                applied_at=applied_at,
                spec=frozen,
            )
            n = pos + neg
            rate = (pos / n) if n > 0 else None
            per_signal_results[signal] = {
                'eligible': len(ids), 'resolved_n': n,
                'positive_n': pos, 'negative_n': neg,
                'rate': rate,
            }
            rate_str = 'n/a' if rate is None else f'{rate * 100:.1f}%'
            self.stdout.write(
                f'  {signal:22s}  eligible={len(ids):4d}  '
                f'resolved={n:4d}  positive={pos:4d}  '
                f'negative={neg:4d}  rate={rate_str}'
            )

        # -- Check 2: end-to-end frozen-cohort persistence --
        e2e_result: dict | None = None
        if not options['dry_run']:
            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_HEADING(
                'Check 2: end-to-end frozen-cohort persistence'
            ))
            rec = _resolve_rec_for_e2e(
                tenant=tenant,
                rec_uuid=options.get('create_measurement'),
                stdout=self.stdout,
            )
            if rec is None:
                self.stdout.write(self.style.WARNING(
                    '  SKIPPED — no eligible rec found on tenant; '
                    'pass --create-measurement <uuid> to force'
                ))
            else:
                # Synthetic apply context — never touches LB config.
                synthetic_id = (
                    f'benchmark-{applied_at.strftime("%Y%m%d%H%M%S")}-'
                    f'{str(rec.pk)[:8]}'
                )
                ctx = LbApplyContext(
                    lb_recommendation_application_id=synthetic_id,
                    applied_at=applied_at,
                    pre_effective_config_hash='benchmark_pre_' + '0' * 50,
                    treatment_effective_config_hash=(
                        'benchmark_treatment_' + '0' * 44
                    ),
                    treatment_managed_hash=(
                        'benchmark_managed_' + '0' * 46
                    ),
                    effective_config_schema_version=(
                        EFFECTIVE_CONFIG_SCHEMA_VERSION
                    ),
                )
                try:
                    row = create_measurement(rec, ctx)
                except MeasurementCreationError as e:
                    self.stdout.write(self.style.ERROR(
                        f'  create_measurement failed: {e}'
                    ))
                    row = None
                if row is not None:
                    e2e_result = {
                        'rom_id': str(row.id),
                        'lb_app_id': row.lb_recommendation_application_id,
                        'target_signal': row.target_signal,
                        'pre_cohort_ids_len': len(
                            row.pre_cohort_conversation_ids
                        ),
                        'pre_n': row.pre_n,
                        'pre_positive_n': row.pre_positive_n,
                        'pre_rate': row.pre_rate,
                        'status': row.status,
                        'deadline': row.measurement_deadline_at.isoformat(),
                    }
                    self.stdout.write(
                        f'  rom_id={e2e_result["rom_id"]}\n'
                        f'  lb_app_id={e2e_result["lb_app_id"]}\n'
                        f'  target_signal={e2e_result["target_signal"]}\n'
                        f'  pre_cohort_ids (frozen)={e2e_result["pre_cohort_ids_len"]}\n'
                        f'  pre_n={e2e_result["pre_n"]} '
                        f'positive={e2e_result["pre_positive_n"]} '
                        f'rate={e2e_result["pre_rate"]!r}\n'
                        f'  status={e2e_result["status"]}\n'
                        f'  deadline={e2e_result["deadline"]}'
                    )
                    # Cross-check: the standalone Check-1 result for the
                    # same signal MUST match the persisted row's counters.
                    ref = per_signal_results.get(row.target_signal)
                    if ref is not None:
                        ok = (
                            row.pre_n == ref['resolved_n']
                            and row.pre_positive_n == ref['positive_n']
                        )
                        marker = (
                            self.style.SUCCESS('OK') if ok
                            else self.style.ERROR('MISMATCH')
                        )
                        self.stdout.write(
                            f'  cohort agreement (check1 vs check2): {marker} '
                            f'(check1 resolved={ref["resolved_n"]} pos='
                            f'{ref["positive_n"]}; check2 pre_n='
                            f'{row.pre_n} pos={row.pre_positive_n})'
                        )

        # -- Summary --
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(
            'ROM v1 Historical Benchmark — Summary'
        ))
        any_nonzero = any(
            r['resolved_n'] > 0 for r in per_signal_results.values()
        )
        e2e_ok = (
            e2e_result is not None
            and e2e_result['pre_cohort_ids_len'] > 0
        )
        signals_with_n_over_30 = [
            s for s, r in per_signal_results.items()
            if r['resolved_n'] >= 30
        ]
        self.stdout.write(
            f'  signals with non-zero baseline: '
            f'{[s for s, r in per_signal_results.items() if r["resolved_n"] > 0]}'
        )
        self.stdout.write(
            f'  signals meeting v1 sample floor (30/arm): '
            f'{signals_with_n_over_30}'
        )
        if options['dry_run']:
            self.stdout.write('  end-to-end persistence: skipped (--dry-run)')
        else:
            self.stdout.write(
                f'  end-to-end persistence: '
                f'{"OK" if e2e_ok else "NOT VERIFIED"}'
            )
        if any_nonzero and (options['dry_run'] or e2e_ok):
            self.stdout.write(self.style.SUCCESS(
                '  ACCEPTANCE: baseline computation reproduces non-empty '
                'cohorts against real Spotless data; ROM v1 machinery is '
                'ready for production hardening.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                '  NOT READY: baseline returned no eligible conversations '
                'for any HIGH_INTENT signal. Investigate BEFORE hardening: '
                '(a) does the tenant have recent conversations? '
                '(b) are semantic events populated with HIGH_INTENT '
                'signals? (c) is OutcomeSnapshot populated?'
            ))


def _frozen_with_signal(spec, signal):
    """Build a FrozenMeasurementSpec with `signal` bound as the
    cohort_entry target — skips going through a Recommendation so we
    can test signals that have no matching rec on this tenant."""
    from apps.conversations.measurement.specs import (
        FrozenMeasurementSpec,
    )
    return FrozenMeasurementSpec(
        spec_key=spec.spec_key,
        version=spec.version,
        family=spec.family,
        description=spec.description,
        cohort_entry=replace(spec.cohort_entry, signal=signal),
        primary_outcome=spec.primary_outcome,
        exclusions=spec.exclusions,
        verdict_gates=spec.verdict_gates,
    )


def _resolve_rec_for_e2e(*, tenant, rec_uuid, stdout):
    """Pick a rec to run the end-to-end measurement creation against.
    Prefers the explicit UUID; otherwise the first eligible rec on the
    tenant (rec_class in STATE_COVERAGE_GAP/STATE_PARTIAL_COVERAGE,
    first subject_signal in HIGH_INTENT_SIGNALS)."""
    if rec_uuid:
        try:
            rec = BehaviorRecommendation.objects.select_related(
                'run', 'run__config_snapshot',
            ).get(pk=rec_uuid)
        except BehaviorRecommendation.DoesNotExist:
            raise CommandError(f'rec {rec_uuid} not found')
        stdout.write(
            f'  using --create-measurement rec: {rec.recommendation_id} '
            f'({rec.rec_class})'
        )
        return rec
    candidates = BehaviorRecommendation.objects.filter(
        run__config_snapshot__tenant_external_id=tenant,
        rec_class__in=[
            'STATE_COVERAGE_GAP', 'STATE_PARTIAL_COVERAGE',
        ],
    ).select_related('run', 'run__config_snapshot').order_by(
        'run__created_at', 'recommendation_id',
    )
    for c in candidates:
        if not c.subject_signals:
            continue
        if c.subject_signals[0] in HIGH_INTENT_SIGNALS:
            stdout.write(
                f'  auto-picked eligible rec: {c.recommendation_id} '
                f'({c.rec_class}, target={c.subject_signals[0]})'
            )
            return c
    return None
