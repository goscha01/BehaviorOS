"""Tests for the import_quo_conversations management command.

Uses the bundled Quo fixtures as the record source (adapter fixture backend)
and in-memory LB/SF resolvers (default when --use-http-resolvers is off).
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.accounts.models import Organization
from apps.context.models import EvidenceEvent
from apps.conversations.models import (
    Conversation,
    ConversationTurn,
    OutcomeSnapshot,
)


class ImportCommandTests(TestCase):
    def test_org_required(self):
        with self.assertRaises(CommandError):
            call_command('import_quo_conversations')

    def test_unknown_org_raises(self):
        with self.assertRaises(CommandError):
            call_command(
                'import_quo_conversations',
                '--org', '00000000-0000-0000-0000-000000000000',
            )

    def test_happy_path_processes_fixtures(self):
        org = Organization.objects.create(name='Spotless')
        out = StringIO()
        call_command(
            'import_quo_conversations',
            '--org', str(org.id),
            '--import-run-id', 'test-run-happy',
            stdout=out,
        )
        text = out.getvalue()
        self.assertIn('fetched', text)
        self.assertIn('created', text)
        self.assertIn('unmatched', text)

        # 8 fixture files, but one is a duplicate of another → 7 unique
        # source_conversation_ids → 7 Conversation rows.
        # Some fixtures don't produce any turns (empty conversation) and get
        # skipped by the normalizer. That's fine — we just verify rows exist.
        self.assertGreater(Conversation.objects.count(), 0)
        # Every persisted conversation should have at least one turn.
        for conv in Conversation.objects.all():
            self.assertGreater(conv.turns.count(), 0)

        # No LB/SF matches expected — in-memory resolvers weren't populated.
        # Every persisted conversation still emits an EvidenceEvent.
        self.assertEqual(
            EvidenceEvent.objects.filter(org=org).count(),
            Conversation.objects.filter(org=org).count(),
        )
        # All events carry the import_run_id in payload.provenance.
        event = EvidenceEvent.objects.filter(org=org).first()
        self.assertEqual(
            event.payload['provenance']['import_run_id'], 'test-run-happy',
        )

    def test_limit_caps_processed_records(self):
        org = Organization.objects.create(name='Spotless')
        out = StringIO()
        call_command(
            'import_quo_conversations',
            '--org', str(org.id),
            '--limit', '2',
            stdout=out,
        )
        self.assertLessEqual(Conversation.objects.count(), 2)

    def test_dry_run_rolls_back_writes(self):
        org = Organization.objects.create(name='Spotless')
        out = StringIO()
        call_command(
            'import_quo_conversations',
            '--org', str(org.id),
            '--dry-run',
            stdout=out,
        )
        self.assertIn('DRY RUN', out.getvalue())
        # Nothing persisted.
        self.assertEqual(Conversation.objects.count(), 0)
        self.assertEqual(ConversationTurn.objects.count(), 0)
        self.assertEqual(OutcomeSnapshot.objects.count(), 0)
        self.assertEqual(EvidenceEvent.objects.filter(org=org).count(), 0)

    def test_rerun_is_idempotent(self):
        org = Organization.objects.create(name='Spotless')
        call_command(
            'import_quo_conversations', '--org', str(org.id),
            stdout=StringIO(),
        )
        first_conv_count = Conversation.objects.count()
        first_turn_count = ConversationTurn.objects.count()
        first_event_count = EvidenceEvent.objects.filter(org=org).count()

        # Second identical run — no new rows.
        call_command(
            'import_quo_conversations', '--org', str(org.id),
            stdout=StringIO(),
        )
        self.assertEqual(Conversation.objects.count(), first_conv_count)
        self.assertEqual(ConversationTurn.objects.count(), first_turn_count)
        self.assertEqual(
            EvidenceEvent.objects.filter(org=org).count(), first_event_count,
        )

    def test_invalid_since_format_errors(self):
        org = Organization.objects.create(name='Spotless')
        with self.assertRaises(CommandError):
            call_command(
                'import_quo_conversations',
                '--org', str(org.id),
                '--since', 'not-a-date',
                stdout=StringIO(),
            )
