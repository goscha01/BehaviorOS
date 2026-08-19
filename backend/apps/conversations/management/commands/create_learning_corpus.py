"""Freeze a LearningCorpus from the current Conversation rows.

Corpus membership is captured at freeze time from a stable query. Once
frozen, adding new conversations does NOT change the corpus — that's
the whole point (bump `--version` to create a new snapshot).

Usage:
    python manage.py create_learning_corpus \\
        --org <uuid> \\
        --name spotless_lb_quo \\
        --version v1 \\
        --min-turns 5 \\
        --require-lb-link
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import Organization
from apps.conversations.models import (
    Conversation, LearningCorpus, LearningCorpusMember, TargetSystem,
)


class Command(BaseCommand):
    help = 'Freeze a LearningCorpus from Conversation state.'

    def add_arguments(self, parser):
        parser.add_argument('--org', required=True)
        parser.add_argument('--name', required=True)
        parser.add_argument('--corpus-version', required=True,
                            help='e.g. v1 — not --version to avoid Django flag collision')
        parser.add_argument('--min-turns', type=int, default=5,
                            help='Include only conversations with >=N turns')
        parser.add_argument('--require-lb-link', action='store_true',
                            help='Include only conversations with at least one LeadBridge EntityLink')
        parser.add_argument('--notes', default='')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(pk=options['org'])
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {options["org"]} not found') from exc

        name = options['name']
        version = options['corpus_version']
        min_turns = options['min_turns']
        require_lb = options['require_lb_link']

        # Reject re-creation of the same (org, name, version).
        if LearningCorpus.objects.filter(org=org, name=name, version=version).exists():
            raise CommandError(
                f'Corpus {name}@{version} already exists for org={org.id}. '
                f'Bump --version to create a new snapshot.'
            )

        # Select conversations. Aggregation done at query time so the
        # freeze is a single SELECT.
        from django.db.models import Count
        qs = Conversation.objects.filter(org=org).annotate(
            turn_count=Count('turns'),
        ).filter(turn_count__gte=min_turns)
        if require_lb:
            qs = qs.filter(
                entity_links__target_system=TargetSystem.LEADBRIDGE,
            ).distinct()
        conversations = list(qs.prefetch_related(
            'entity_links', 'outcome_snapshots',
        ))

        selection = {
            'source': 'lb_anchored',
            'min_turns': min_turns,
            'require_lb_link': require_lb,
            'notes': options['notes'],
        }

        with transaction.atomic():
            corpus = LearningCorpus.objects.create(
                org=org, name=name, version=version,
                selection_criteria=selection,
                member_count=len(conversations),
            )
            for conv in conversations:
                lb_link = conv.entity_links.filter(
                    target_system=TargetSystem.LEADBRIDGE,
                ).first()
                latest_snap = conv.outcome_snapshots.order_by('-captured_at').first()
                LearningCorpusMember.objects.create(
                    corpus=corpus,
                    conversation=conv,
                    lb_lead_id=(lb_link.target_id if lb_link else ''),
                    lb_status_at_freeze=(latest_snap.lb_status if latest_snap else ''),
                    turn_count_at_freeze=conv.turn_count,
                )

        self.stdout.write(self.style.SUCCESS(
            f'Corpus frozen: {name}@{version} id={corpus.id} members={len(conversations)}'
        ))
