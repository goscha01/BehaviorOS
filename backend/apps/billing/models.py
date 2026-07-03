from django.db import models
from apps.common.models import BaseModel


class StripeCustomer(BaseModel):
    org = models.OneToOneField(
        'accounts.Organization', on_delete=models.CASCADE, related_name='stripe_customer'
    )
    stripe_customer_id = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return f"{self.org.name} - {self.stripe_customer_id}"


class Subscription(BaseModel):
    class Plan(models.TextChoices):
        STARTER = 'starter', 'Starter'
        PRO = 'pro', 'Pro'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past Due'
        CANCELED = 'canceled', 'Canceled'
        INCOMPLETE = 'incomplete', 'Incomplete'
        TRIALING = 'trialing', 'Trialing'

    org = models.OneToOneField(
        'accounts.Organization', on_delete=models.CASCADE, related_name='subscription'
    )
    stripe_subscription_id = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INCOMPLETE)
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.STARTER)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)

    @property
    def is_active(self):
        return self.status in (self.Status.ACTIVE, self.Status.TRIALING)

    def __str__(self):
        return f"{self.org.name} - {self.plan} ({self.status})"
