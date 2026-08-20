"""Service-token auth for the BehaviorOS Insights read API.

LeadBridge (and future runtime consumers) authenticate with a shared
bearer token from `BEHAVIOR_CONTEXT_SERVICE_TOKEN` — reuses the same
setting the Context API already uses, no new credential surface.

Tenant scoping is enforced at the ViewSet layer via the `tenantId`
query parameter (LB `userId` / external tenant id stored on the
underlying TenantConfigSnapshot). The auth layer only decides
"is this a legitimate service caller"; it does NOT decide which
tenant the caller may read.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import authentication, exceptions


class InsightsServiceUser:
    is_authenticated = True
    is_active = True
    is_anonymous = False
    is_staff = False
    is_superuser = False
    username = 'behavioros-insights-runtime'
    pk = None

    def __str__(self):
        return self.username


class InsightsServiceTokenAuthentication(authentication.BaseAuthentication):
    """Mirror of `apps.context.auth.ServiceTokenAuthentication` for the
    insights API. Kept separate so we can later split tokens per
    consumer without churning the context path."""

    keyword = 'Bearer'
    header_name = 'HTTP_AUTHORIZATION'

    def authenticate(self, request):
        # Prefer a dedicated insights token if set; otherwise fall back
        # to the shared BEHAVIOR_CONTEXT_SERVICE_TOKEN so v1 deployments
        # don't need to configure two tokens.
        expected = (
            getattr(settings, 'BEHAVIOR_OS_INSIGHTS_TOKEN', '')
            or getattr(settings, 'BEHAVIOR_CONTEXT_SERVICE_TOKEN', '')
        )
        header = request.META.get(self.header_name, '')
        provided = ''
        if header.startswith(self.keyword + ' '):
            provided = header[len(self.keyword) + 1:].strip()
        if expected and provided != expected:
            raise exceptions.AuthenticationFailed(
                'Invalid or missing service token.',
            )
        return (InsightsServiceUser(), None)

    def authenticate_header(self, request):
        return self.keyword
