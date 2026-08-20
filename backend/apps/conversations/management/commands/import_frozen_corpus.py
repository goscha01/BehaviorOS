"""Import a frozen LearningCorpus export produced by export_frozen_corpus.

Insert-only semantics — never overwrites existing rows. If a row's UUID
already exists in the destination, it's skipped (idempotent re-import).
If a natural-key collision exists with a DIFFERENT UUID, the row is
skipped and reported as a conflict — the importer never silently
resolves such mismatches because they usually mean two environments
independently created the same logical entity and manual reconciliation
is required.

For "frozen" corpora this insert-only stance is exactly right:
- Reproducibility: identical export → identical DB state.
- Safety: never mutates unrelated production rows.
- Auditability: conflicts are surfaced, not hidden.

Usage:
    python manage.py import_frozen_corpus \\
        --file ./tmp/corpus_export.json \\
        [--dry-run]

Prints a per-table breakdown of inserted / skipped-existing / conflict.
Exits non-zero on any conflict.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from apps.accounts.models import Organization
from apps.conversations.models import (
    CandidatePattern, Conversation, ConversationSemanticEvent,
    ConversationTurn, EntityLink, LearningCorpus, LearningCorpusMember,
    OutcomeSnapshot, PatternDiscoveryRun, SemanticExtractionRun,
)


# Natural-key tuples per model. Used to detect UUID-vs-natural-key
# collisions on import. If a row with the same natural key already
# exists but a DIFFERENT UUID, the importer refuses to insert.
NATURAL_KEYS = {
    'organization': ('name',),  # no unique constraint on name, best-effort
    'learning_corpus': ('org_id', 'name', 'version'),
    'conversations': ('org_id', 'source', 'source_conversation_id'),
    'conversation_turns': ('conversation_id', 'source_turn_id'),
    'entity_links': ('conversation_id', 'target_system', 'target_type',
                      'target_id', 'match_method'),
    'outcome_snapshots': ('conversation_id', 'captured_at'),
    'learning_corpus_members': ('corpus_id', 'conversation_id'),
    'semantic_extraction_runs': ('corpus_id', 'extractor_version',
                                  'ontology_version', 'prompt_version', 'model'),
    'conversation_semantic_events': ('extraction_run_id', 'conversation_id', 'ordinal'),
    'pattern_discovery_runs': ('corpus_id', 'extraction_run_id',
                                'analyzer_version', 'split_seed'),
    'candidate_patterns': ('discovery_run_id', 'pattern_id'),
}

# Import order — parents before children. Organization first since
# everything hangs off it; corpus before members; extraction run before
# events; discovery run before candidate patterns.
IMPORT_ORDER = [
    ('organization', Organization),
    ('conversations', Conversation),
    ('conversation_turns', ConversationTurn),
    ('entity_links', EntityLink),
    ('outcome_snapshots', OutcomeSnapshot),
    ('learning_corpus', LearningCorpus),
    ('learning_corpus_members', LearningCorpusMember),
    ('semantic_extraction_runs', SemanticExtractionRun),
    ('conversation_semantic_events', ConversationSemanticEvent),
    ('pattern_discovery_runs', PatternDiscoveryRun),
    ('candidate_patterns', CandidatePattern),
]


def _iter_rows(payload: dict, key: str):
    """Wrap single-object keys (organization, learning_corpus) uniformly."""
    v = payload.get(key)
    if v is None:
        return []
    if isinstance(v, dict):
        return [v]
    return v


class Command(BaseCommand):
    help = 'Insert-only import of a frozen LearningCorpus export.'

    def add_arguments(self, parser):
        parser.add_argument('--file', required=True,
                            help='Path to the export JSON')
        parser.add_argument('--dry-run', action='store_true',
                            help='Compute inserts/skips/conflicts without writing')

    def handle(self, *args, **options):
        path = Path(options['file'])
        if not path.exists():
            raise CommandError(f'{path} not found')
        with path.open('r', encoding='utf-8') as f:
            payload = json.load(f)

        # Verify checksum if present
        expected_checksum = payload.get('meta', {}).get('checksum_sha256')
        if expected_checksum:
            # Recompute against the payload with checksum removed — matches
            # the exporter's semantics (checksum was appended to meta AFTER
            # the deterministic dump was taken).
            meta_copy = dict(payload['meta'])
            meta_copy.pop('checksum_sha256', None)
            payload_copy = dict(payload)
            payload_copy['meta'] = meta_copy
            raw = json.dumps(payload_copy, sort_keys=True, indent=2)
            actual = hashlib.sha256(raw.encode('utf-8')).hexdigest()
            if actual != expected_checksum:
                self.stdout.write(self.style.WARNING(
                    f'checksum mismatch: expected {expected_checksum[:12]}... '
                    f'got {actual[:12]}...  (proceeding, but export may be corrupt)'
                ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f'checksum ok: sha256={actual[:12]}...'
                ))

        meta = payload.get('meta', {})
        self.stdout.write(self.style.NOTICE(
            f'Importing corpus={meta.get("corpus_name")}@{meta.get("corpus_version")} '
            f'exported_at={meta.get("exported_at")}'
        ))
        self.stdout.write(f'  expected counts: {meta.get("counts", {})}')

        summary: dict[str, dict[str, int]] = {}
        conflicts: list[str] = []

        # Wrap the entire import in a single transaction so a mid-import
        # failure leaves prod exactly as we found it.
        with transaction.atomic():
            for key, model_cls in IMPORT_ORDER:
                rows = _iter_rows(payload, key)
                summary[key] = {'inserted': 0, 'skipped_existing': 0, 'conflict': 0}
                if not rows:
                    continue
                self.stdout.write(f'  {key}: {len(rows)} rows to consider')
                nat_key_fields = NATURAL_KEYS.get(key, ())
                to_insert = []
                for row in rows:
                    uid = row['id']
                    if model_cls.objects.filter(pk=uid).exists():
                        summary[key]['skipped_existing'] += 1
                        continue
                    if nat_key_fields:
                        nat_filter = {f: row.get(f) for f in nat_key_fields}
                        clash = model_cls.objects.filter(**nat_filter).exclude(pk=uid).first()
                        if clash is not None:
                            summary[key]['conflict'] += 1
                            conflicts.append(
                                f'{key}: incoming id={uid} natural_key={nat_filter} '
                                f'clashes with existing id={clash.pk}'
                            )
                            continue
                    to_insert.append(_build_instance(model_cls, row))
                if to_insert and not options['dry_run']:
                    # bulk_create bypasses auto_now / auto_now_add, so
                    # created_at + updated_at from the export land as-is.
                    model_cls.objects.bulk_create(to_insert, batch_size=500)
                summary[key]['inserted'] = len(to_insert)
            if options['dry_run']:
                self.stdout.write(self.style.WARNING('--dry-run: rolling back'))
                transaction.set_rollback(True)

        # Report
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Import summary'))
        for key, counts in summary.items():
            total = sum(counts.values())
            if total == 0:
                continue
            self.stdout.write(
                f'  {key:35}  '
                f'inserted={counts["inserted"]:>5}  '
                f'skipped={counts["skipped_existing"]:>5}  '
                f'conflict={counts["conflict"]:>5}'
            )
        if conflicts:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR(f'{len(conflicts)} conflict(s):'))
            for c in conflicts[:20]:
                self.stdout.write(f'  - {c}')
            if len(conflicts) > 20:
                self.stdout.write(f'  ... {len(conflicts) - 20} more')
            raise CommandError('unresolved conflicts; import aborted')


def _build_instance(model_cls, row: dict):
    """Build a model instance from a dict of exported field values.

    Coerces ISO datetime strings back to `datetime` objects for
    DateTimeField columns; other types round-trip fine via JSON.
    Only fields that exist on the model concrete columns are passed
    in — extras from the export are ignored.
    """
    concrete_fields = {
        f.attname if getattr(f, 'attname', None) else f.name: f
        for f in model_cls._meta.get_fields()
        if not (f.is_relation and (f.many_to_many or f.one_to_many))
    }
    clean = {}
    for k, v in row.items():
        f = concrete_fields.get(k)
        if f is None:
            continue
        if isinstance(f, models.DateTimeField) and isinstance(v, str):
            v = datetime.fromisoformat(v)
        clean[k] = v
    return model_cls(**clean)
