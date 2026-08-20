"""Pipeline 1B-4: fetch a LeadBridge tenant's effective configuration
and persist it as an immutable TenantConfigSnapshot.

Usage:
    python manage.py fetch_lb_config \\
        --org <behavioros-org-uuid> \\
        --tenant <lb-user-uuid> \\
        [--service-group house_cleaning]

Idempotent by content hash — re-running when the tenant's config hasn't
changed prints "no change" and does not create a new snapshot row. This
preserves the audit trail (every write is a genuine change) and keeps
snapshot count bounded.
"""

from __future__ import annotations

import hashlib
import json

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Organization
from apps.conversations.models import TenantConfigSnapshot
from apps.conversations.services.lb_config_client import LbConfigClient


class Command(BaseCommand):
    help = 'Fetch + persist a LeadBridge tenant config snapshot (Pipeline 1B-4).'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True,
                            help='BehaviorOS Organization UUID')
        parser.add_argument('--tenant', required=True,
                            help='LB User UUID (lb_user_id / tenantId)')
        parser.add_argument('--service-group', default='',
                            help='Optional. Restricts ServiceProfiles to a group. '
                                 'Accepts LB canonical values (cleaning, carpet, ...) '
                                 'or the alias "house_cleaning". '
                                 'Default: no filter (all groups).')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(pk=options['org'])
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {options["org"]} not found') from exc

        tenant_id = options['tenant']
        service_group = options['service_group'] or ''

        client = LbConfigClient()
        self.stdout.write(self.style.NOTICE(
            f'Fetching LB config: tenant={tenant_id} '
            f'service_group={service_group or "(all)"}'
        ))
        try:
            raw = client.fetch(
                tenant_id=tenant_id,
                service_group=service_group or None,
            )
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc

        # Content hash for idempotency + drift detection.
        canonical = json.dumps(raw, sort_keys=True, separators=(',', ':'))
        sha = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        self.stdout.write(
            f'  contract_version={raw.get("contract_version")} '
            f'sha256={sha[:12]}...  '
            f'saved_accounts={len(raw.get("saved_accounts", []))} '
            f'service_profiles={len(raw.get("service_profiles", []))}'
        )

        # Idempotency check — snapshot with same content already exists?
        existing = TenantConfigSnapshot.objects.filter(
            org=org,
            source_system=TenantConfigSnapshot.SourceSystem.LEADBRIDGE,
            tenant_external_id=tenant_id,
            service_group=service_group,
            raw_config_sha256=sha,
        ).first()
        if existing:
            self.stdout.write(self.style.WARNING(
                f'no change since snapshot {existing.pk} '
                f'(captured {existing.created_at.isoformat()}) — skipping insert'
            ))
            return

        snapshot = TenantConfigSnapshot.objects.create(
            org=org,
            source_system=TenantConfigSnapshot.SourceSystem.LEADBRIDGE,
            tenant_external_id=tenant_id,
            service_group=service_group,
            contract_version=raw.get('contract_version', ''),
            raw_config=raw,
            raw_config_sha256=sha,
            fetched_from_url=client.endpoint,
        )
        self.stdout.write(self.style.SUCCESS(
            f'wrote snapshot {snapshot.pk}'
        ))
