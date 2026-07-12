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

        self.stdout.write(self.style.SUCCESS(
            f'bootstrap_admin: user={username} ({"created" if user_created else "updated"}) '
            f'org={org_name} ({"created" if org_created else "exists"}) '
            f'membership={"created" if mem_created else "exists"}'
        ))

        # Diagnostic — dump all orgs + this user's memberships + suggestion counts.
        from apps.learning.models import LearningSuggestion, LearningJob
        self.stdout.write('--- DIAG ---')
        for o in Organization.objects.all():
            n_sugg = LearningSuggestion.objects.filter(org=o).count()
            n_jobs = LearningJob.objects.filter(org=o).count()
            self.stdout.write(f'  org id={o.id} name={o.name!r} suggestions={n_sugg} jobs={n_jobs}')
        for m in Membership.objects.filter(user=user).select_related('org'):
            self.stdout.write(f'  membership user={user.username} → org id={m.org.id} name={m.org.name!r} role={m.role}')
        self.stdout.write(f'  bootstrap_admin used org id={org.id}')
        self.stdout.write('--- /DIAG ---')
