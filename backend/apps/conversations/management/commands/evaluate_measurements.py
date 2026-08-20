"""Re-evaluate RecommendationOutcomeMeasurement rows (ROM v1 Step 5).

Iterates all non-terminal measurements and calls the deterministic
evaluator. Safe to run at any cadence — repeated runs against the
same corpus produce byte-identical output.

Usage:
    python manage.py evaluate_measurements
    python manage.py evaluate_measurements --measurement <uuid>
    python manage.py evaluate_measurements --tenant <lb-user-uuid>

Callable from Celery Beat / cron, or manually for a single row.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.conversations.measurement.evaluator import evaluate
from apps.conversations.models import RecommendationOutcomeMeasurement


class Command(BaseCommand):
    help = 'Re-evaluate outcome measurements (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--measurement',
            help='Single measurement UUID (default: all non-terminal)',
        )
        parser.add_argument(
            '--tenant',
            help='Restrict to one tenant_external_id (LB userId)',
        )

    def handle(self, *args, **options):
        qs = RecommendationOutcomeMeasurement.objects.filter(
            finalized_at__isnull=True,
        )
        if options.get('measurement'):
            qs = qs.filter(pk=options['measurement'])
        if options.get('tenant'):
            qs = qs.filter(tenant_external_id=options['tenant'])
        total = qs.count()
        self.stdout.write(f'Evaluating {total} non-terminal measurements')
        for m in qs.select_related('recommendation'):
            before = m.status
            updated = evaluate(m)
            self.stdout.write(
                f'  {updated.id}: {before} → {updated.status} '
                f'({updated.status_reason})'
            )
        self.stdout.write(self.style.SUCCESS(f'done: {total}'))
