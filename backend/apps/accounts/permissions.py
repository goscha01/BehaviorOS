from rest_framework.permissions import BasePermission

from apps.accounts.models import Membership


def _ensure_org(request):
    """Populate request.org if middleware missed it.

    OrgContextMiddleware runs before DRF authenticates JWTs, so
    request.user is still AnonymousUser when it fires. By the time
    DRF permissions run, request.user IS the JWT-authenticated user
    — so we resolve the primary membership here as a fallback.
    Safe to call multiple times.
    """
    if getattr(request, 'org', None):
        return request.org
    if not (request.user and request.user.is_authenticated):
        return None
    membership = (
        Membership.objects.filter(user=request.user)
        .select_related('org')
        .first()
    )
    request.org = membership.org if membership else None
    return request.org


class IsOrgMember(BasePermission):
    def has_permission(self, request, view):
        return _ensure_org(request) is not None


class IsOrgAdmin(BasePermission):
    def has_permission(self, request, view):
        org = _ensure_org(request)
        if org is None:
            return False
        return Membership.objects.filter(
            org=org, user=request.user, role__in=['owner', 'admin']
        ).exists()


class HasActiveSubscription(BasePermission):
    message = 'An active subscription is required to access this feature.'

    def has_permission(self, request, view):
        org = _ensure_org(request)
        if org is None:
            return False
        return hasattr(org, 'subscription') and org.subscription.is_active
