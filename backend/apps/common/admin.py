from django.contrib import admin
from apps.common.audit import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('action', 'object_type', 'user', 'org', 'created_at')
    list_filter = ('action', 'object_type')
    search_fields = ('action', 'object_type', 'user__username', 'org__name')
    readonly_fields = ('org', 'user', 'action', 'object_type', 'object_id', 'metadata', 'created_at')
