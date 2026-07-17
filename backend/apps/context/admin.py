from django.contrib import admin

from apps.context.models import (
    ContextRequestLog,
    CustomerHistoryAggregate,
    EvidenceEvent,
    OrgStatistics,
)


@admin.register(ContextRequestLog)
class ContextRequestLogAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'runtime', 'event_type', 'status',
        'confidence', 'latency_ms', 'returned_to_runtime',
    )
    list_filter = ('runtime', 'status', 'returned_to_runtime', 'event_type')
    search_fields = ('tenant_id', 'customer_id', 'lead_id', 'conversation_id')
    readonly_fields = tuple(
        f.name for f in ContextRequestLog._meta.get_fields()
        if hasattr(f, 'name') and not f.many_to_many
    )


@admin.register(EvidenceEvent)
class EvidenceEventAdmin(admin.ModelAdmin):
    list_display = (
        'occurred_at', 'source_kind', 'runtime', 'event_type',
        'customer_id', 'conversation_id',
    )
    list_filter = ('source_kind', 'runtime', 'event_type', 'channel')
    search_fields = (
        'customer_id', 'lead_id', 'conversation_id', 'external_id',
        'message_excerpt',
    )
    readonly_fields = tuple(
        f.name for f in EvidenceEvent._meta.get_fields()
        if hasattr(f, 'name') and not f.many_to_many
    )


@admin.register(CustomerHistoryAggregate)
class CustomerHistoryAggregateAdmin(admin.ModelAdmin):
    list_display = ('customer_id', 'total_events', 'first_seen_at', 'last_seen_at')
    search_fields = ('customer_id',)
    readonly_fields = tuple(
        f.name for f in CustomerHistoryAggregate._meta.get_fields()
        if hasattr(f, 'name') and not f.many_to_many
    )


@admin.register(OrgStatistics)
class OrgStatisticsAdmin(admin.ModelAdmin):
    list_display = ('org', 'total_events', 'last_event_at')
    readonly_fields = tuple(
        f.name for f in OrgStatistics._meta.get_fields()
        if hasattr(f, 'name') and not f.many_to_many
    )
