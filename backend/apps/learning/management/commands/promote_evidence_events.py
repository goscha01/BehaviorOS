"""Promote EvidenceEvent rows into EvidenceInsight rows.

Usage:
    # Promote every unpromoted runtime event for one org.
    python manage.py promote_evidence_events --org <uuid>

    # Cap the batch.
    python manage.py promote_evidence_events --org <uuid> --limit 50

    # Promote for all orgs (fans out per-org so one bad org doesn't
    # stop the others).
    python manage.py promote_evidence_events --all-orgs --limit 200

Idempotent by design — reruns pick up whatever the last run left
behind (see EvidenceEvent.promoted_at and EvidenceInsight uniqueness).
"""

from __future__ import annotations

import sys

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Organization
from apps.learning.services.promotion import PromotionResult, promote_evidence_events


class Command(BaseCommand):
    help = 'Promote unpromoted EvidenceEvent rows into EvidenceInsight rows.'

    def add_arguments(self, parser) -> None:
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--org', type=str, help='Organization UUID.')
        group.add_argument(
            '--all-orgs', action='store_true',
            help='Promote for every Organization in the DB.',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Max events per org to promote in this run.',
        )

    def handle(self, *args, **opts) -> None:
        limit: int | None = opts['limit']

        if opts.get('all_orgs'):
            orgs = list(Organization.objects.all())
            if not orgs:
                self.stdout.write('No organizations found.')
                return
        else:
            org_id = opts['org']
            try:
                org = Organization.objects.get(pk=org_id)
            except (Organization.DoesNotExist, ValidationError, ValueError) as exc:
                raise CommandError(f'Organization not found: {org_id}') from exc
            orgs = [org]

        exit_code = 0
        for org in orgs:
            result: PromotionResult = promote_evidence_events(org=org, limit=limit)
            skip_summary = (
                ' '.join(f'{k}={v}' for k, v in sorted(result.skipped_by_reason.items()))
                or '(none)'
            )
            self.stdout.write(
                f'org={org.id} name={org.name!r} '
                f'scanned={result.scanned} '
                f'promoted={result.promoted} '
                f'skipped={result.skipped} '
                f'failed={result.failed} '
                f'skips={skip_summary}'
            )
            for err in result.errors:
                self.stderr.write(f'  ! {err}')
            if result.failed and not result.promoted:
                # Something's structurally wrong for this org — surface
                # a non-zero exit for CI / scheduler visibility, but keep
                # going for the other orgs.
                exit_code = 2

        if exit_code:
            sys.exit(exit_code)
