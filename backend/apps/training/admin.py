from django.contrib import admin
from apps.training.models import (
    BusinessProfile,
    ScenarioTemplate,
    Script,
    TrainingSession,
    SessionTurn,
    SessionResult,
)


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):
    list_display = ('name', 'org', 'created_at')
    search_fields = ('name', 'org__name')


@admin.register(ScenarioTemplate)
class ScenarioTemplateAdmin(admin.ModelAdmin):
    list_display = ('name', 'org', 'difficulty', 'is_default', 'created_at')
    list_filter = ('difficulty', 'is_default')
    search_fields = ('name', 'org__name')


@admin.register(Script)
class ScriptAdmin(admin.ModelAdmin):
    list_display = ('name', 'org', 'version', 'created_at')
    search_fields = ('name', 'org__name')


@admin.register(TrainingSession)
class TrainingSessionAdmin(admin.ModelAdmin):
    list_display = ('id', 'org', 'status', 'started_at', 'ended_at', 'created_at')
    list_filter = ('status',)
    search_fields = ('org__name',)


@admin.register(SessionTurn)
class SessionTurnAdmin(admin.ModelAdmin):
    list_display = ('session', 'speaker', 'created_at')
    list_filter = ('speaker',)


@admin.register(SessionResult)
class SessionResultAdmin(admin.ModelAdmin):
    list_display = ('session', 'outcome', 'created_at')
    list_filter = ('outcome',)
