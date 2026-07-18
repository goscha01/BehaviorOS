"""Idempotent bootstrap: create a superuser + default Organization + Membership from env vars.

Runs on container start (via Dockerfile CMD) so a fresh deploy has a
working login. Reads:

    BOOTSTRAP_USERNAME  (default: admin)
    BOOTSTRAP_EMAIL     (default: admin@behavioros.local)
    BOOTSTRAP_PASSWORD  (required — no default; command is a no-op without it)
    BOOTSTRAP_ORG_NAME  (default: BehaviorOS)

Safe to re-run: each object is get_or_create'd, and the password is only
updated when BOOTSTRAP_PASSWORD is set (so removing the env var later
freezes the current password rather than blanking it).
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.accounts.models import Membership, Organization


class Command(BaseCommand):
    help = 'Create a superuser + default org + membership from BOOTSTRAP_* env vars.'

    def handle(self, *args, **options):
        password = os.environ.get('BOOTSTRAP_PASSWORD', '')
        if not password:
            self.stdout.write('BOOTSTRAP_PASSWORD not set; skipping.')
            return

        username = os.environ.get('BOOTSTRAP_USERNAME', 'admin')
        email = os.environ.get('BOOTSTRAP_EMAIL', 'admin@behavioros.local')
        org_name = os.environ.get('BOOTSTRAP_ORG_NAME', 'BehaviorOS')

        User = get_user_model()
        user, user_created = User.objects.get_or_create(
            username=username,
            defaults={'email': email, 'is_staff': True, 'is_superuser': True},
        )
        # Always ensure staff/superuser flags + refresh password.
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        org, org_created = Organization.objects.get_or_create(name=org_name)
        membership, mem_created = Membership.objects.get_or_create(
            user=user, org=org, defaults={'role': Membership.Role.OWNER}
        )

        # accounts.signals auto-creates an "{username}'s Organization" on user creation.
        # For the single-tenant Phase 1 admin, that personal org is empty and misleads
        # the middleware (which picks the first membership). Prune anything that
        # isn't our canonical org so the user has exactly one membership.
        pruned = Membership.objects.filter(user=user).exclude(org=org).delete()
        # Only sweep the auto-created personal-org pattern. Any real tenant
        # provisioned via admin / API / management command has a bespoke name
        # ("Spotless Homes", "Acme HVAC", …) and MUST survive redeploys even
        # if its first membership hasn't been attached yet — otherwise the
        # bootstrap wipes real customer state on every container boot.
        # See incident 2026-07-18: Spotless Homes org was silently deleted
        # by this sweep during a routine env-var redeploy.
        Organization.objects.exclude(pk=org.pk) \
            .filter(memberships__isnull=True) \
            .filter(name__endswith="'s Organization") \
            .delete()

        self.stdout.write(self.style.SUCCESS(
            f'bootstrap_admin: user={username} ({"created" if user_created else "updated"}) '
            f'org={org_name} ({"created" if org_created else "exists"}) '
            f'membership={"created" if mem_created else "exists"}'
        ))

        self.stdout.write(f'  pruned={pruned[0]} stray memberships/orgs')
