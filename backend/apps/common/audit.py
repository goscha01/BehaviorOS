from django.db import models
from django.conf import settings
from apps.common.models import BaseModel


class AuditLog(BaseModel):
    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='audit_logs'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=100)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.action} on {self.object_type} by {self.user}"


def log_action(org, user, action, object_type, object_id='', metadata=None):
    return AuditLog.objects.create(
        org=org,
        user=user,
        action=action,
        object_type=object_type,
        object_id=str(object_id),
        metadata=metadata or {},
    )
