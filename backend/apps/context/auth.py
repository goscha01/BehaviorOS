"""Service-token auth for POST /v1/context.

Runtimes (LeadBridge / Callio) are server-side apps, not end-user clients.
They authenticate with a shared bearer token from BEHAVIOR_CONTEXT_SERVICE_TOKEN,
not with a per-user JWT. This class returns a lightweight anonymous-service
identity so DRF's IsAuthenticated permission is satisfied without any user
lookup.

Contract:
- Missing token AND no setting configured: allow (dev-mode / local runs).
  Configuring a real deployment sets the setting; forgetting to send it
  from the runtime is what the check catches.
- Missing/wrong token when a setting IS configured: raise 401. This is the
  ONE case where /v1/context returns a non-200 — a runtime misconfiguration
  is a bug, not a "no context" scenario.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import authentication, exceptions


class ServiceTokenUser:
    """DRF requires an object with `.is_authenticated`. We give it just
    enough to pass IsAuthenticated without pretending to be a real user.
    """

    is_authenticated = True
    is_active = True
    is_anonymous = False
    is_staff = False
    is_superuser = False
    username = 'behavioros-runtime'
    pk = None

    def __str__(self):
        return self.username


class ServiceTokenAuthentication(authentication.BaseAuthentication):
    keyword = 'Bearer'
    header_name = 'HTTP_AUTHORIZATION'

    def authenticate(self, request):
        expected = getattr(settings, 'BEHAVIOR_CONTEXT_SERVICE_TOKEN', '')
        header = request.META.get(self.header_name, '')
        provided = ''
        if header.startswith(self.keyword + ' '):
            provided = header[len(self.keyword) + 1:].strip()

        # Server has a token set but caller either omitted or mismatched.
        # This is the ONE case where /v1/context returns a non-200 — a
        # runtime misconfiguration is a bug, not a "no context" scenario.
        if expected and provided != expected:
            raise exceptions.AuthenticationFailed('Invalid or missing service token.')

        # Otherwise authenticate as the runtime service. Covers both:
        #   - expected == '' (dev-mode / not configured): anonymous OK.
        #   - expected != '' AND matched: legitimate runtime call.
        return (ServiceTokenUser(), None)

    def authenticate_header(self, request):
        return self.keyword
