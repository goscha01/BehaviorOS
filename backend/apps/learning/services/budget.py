"""Budget tracking for a learning job.

Keeps a running per-job cost. When the ceiling is exceeded, the orchestrator
marks the job PARTIAL and stops queuing new analyses — insights already
persisted stay persisted, and the resume queue (analyzed_at IS NULL) picks
up remaining work on the next scheduled run.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.db.models import F

from apps.learning.models import LearningJob


@dataclass
class BudgetTracker:
    job: LearningJob
    limit_usd: Decimal = Decimal('0')

    def __post_init__(self) -> None:
        if self.limit_usd == 0:
            self.limit_usd = Decimal(str(settings.LEARNING_JOB_MAX_USD))

    @property
    def spent_usd(self) -> Decimal:
        return Decimal(self.job.cost_usd)

    def remaining_usd(self) -> Decimal:
        return self.limit_usd - self.spent_usd

    def exceeded(self) -> bool:
        return self.spent_usd >= self.limit_usd

    def record(self, additional_usd: Decimal) -> Decimal:
        """Atomically add cost to the job and return the new total.

        Uses F() so concurrent workers can't clobber each other's writes
        when Celery fans this out. Refreshes the in-memory job after.
        """
        LearningJob.objects.filter(pk=self.job.pk).update(
            cost_usd=F('cost_usd') + additional_usd
        )
        self.job.refresh_from_db(fields=['cost_usd'])
        return Decimal(self.job.cost_usd)
