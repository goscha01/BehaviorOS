from rest_framework.permissions import BasePermission
from apps.accounts.models import Membership


class IsOrgMember(BasePermission):
    def has_permission(self, request, view):
        return hasattr(request, 'org') and request.org is not None


class IsOrgAdmin(BasePermission):
    def has_permission(self, request, view):
        if not hasattr(request, 'org') or not request.org:
            return False
        return Membership.objects.filter(
            org=request.org, user=request.user, role__in=['owner', 'admin']
        ).exists()


class HasActiveSubscription(BasePermission):
    message = 'An active subscription is required to access this feature.'

    def has_permission(self, request, view):
        if not hasattr(request, 'org') or not request.org:
            return False
        return hasattr(request.org, 'subscription') and request.org.subscription.is_active
