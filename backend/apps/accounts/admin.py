from django.contrib import admin
from apps.accounts.models import Organization, Membership


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'id', 'created_at')
    search_fields = ('name',)


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'org', 'role', 'created_at')
    list_filter = ('role',)
    search_fields = ('user__username', 'org__name')
