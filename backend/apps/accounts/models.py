from django.db import models
from django.conf import settings
from apps.common.models import BaseModel


class Organization(BaseModel):
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name


class Membership(BaseModel):
    class Role(models.TextChoices):
        OWNER = 'owner', 'Owner'
        ADMIN = 'admin', 'Admin'
        MEMBER = 'member', 'Member'

    org = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='memberships')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships'
    )
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)

    class Meta:
        unique_together = ('org', 'user')

    def __str__(self):
        return f"{self.user} - {self.org} ({self.role})"
