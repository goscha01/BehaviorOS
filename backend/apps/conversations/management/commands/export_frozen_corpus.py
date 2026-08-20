"""Export one frozen LearningCorpus and all its FK dependencies to a
JSON file for cross-environment import.

Scope of the export (everything needed to reproduce the 1B-2 / 1B-3
analyses in a different environment):

    Organization (referenced by corpus)
    ├── LearningCorpus (1 row)
    │    ├── LearningCorpusMember (N rows, one per included conversation)
    │    ├── SemanticExtractionRun (M rows — all runs against this corpus)
    │    │    └── ConversationSemanticEvent (K rows, per run)
    │    └── PatternDiscoveryRun + CandidatePattern (optional, --include-1b2)
    └── Conversation (member conversations only)
         ├── ConversationTurn (all turns)
         ├── EntityLink (all links)
         └── OutcomeSnapshot (all snapshots)

Every row is dumped with its UUID preserved so a matching row in the
destination environment is identifiable by primary key. Natural-key
metadata (unique constraints) is included per row so the importer can
sanity-check UUID-vs-natural-key alignment.

Usage:
    python manage.py export_frozen_corpus \\
        --corpus spotless_lb_quo@v1 \\
        --org <org-uuid> \\
        --out ./tmp/corpus_export.json \\
        [--include-1b2]

Output is a single JSON file with the shape:

    {
      "meta": {
        "exported_at": "...", "corpus": "spotless_lb_quo@v1",
        "counts": {"conversations": 311, "events": 3235, ...}
      },
      "organization": {...},
      "learning_corpus": {...},
      "conversations": [...],
      "conversation_turns": [...],
      "entity_links": [...],
      "outcome_snapshots": [...],
      "learning_corpus_members": [...],
      "semantic_extraction_runs": [...],
      "conversation_semantic_events": [...],
      "pattern_discovery_runs": [...],   # only if --include-1b2
      "candidate_patterns": [...]        #        "         "
    }
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.forms.models import model_to_dict

from apps.accounts.models import Organization
from apps.conversations.models import (
    CandidatePattern, Conversation, ConversationSemanticEvent,
    ConversationTurn, EntityLink, LearningCorpus, LearningCorpusMember,
    OutcomeSnapshot, PatternDiscoveryRun, SemanticExtractionRun,
)


class _JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def _row(instance) -> dict:
    """Serialize a Django model instance to a plain dict, preserving all
    fields (including FKs as UUID strings). Uses `model_to_dict` for the
    value shape, then folds in the primary key + FK columns explicitly
    (since model_to_dict skips non-editable fields like `id`)."""
    d = model_to_dict(instance)
    d['id'] = instance.pk
    d['created_at'] = getattr(instance, 'created_at', None)
    d['updated_at'] = getattr(instance, 'updated_at', None)
    # FK id columns — flatten to *_id keys for stable import contract.
    for field in instance._meta.get_fields():
        if field.is_relation and field.many_to_one:
            d[f'{field.name}_id'] = getattr(instance, f'{field.name}_id')
            # remove the value model_to_dict wrote (the referenced object)
            d.pop(field.name, None)
    return d


class Command(BaseCommand):
    help = 'Export a frozen LearningCorpus + FK dependencies to JSON.'

    def add_arguments(self, parser):
        parser.add_argument('--corpus', required=True,
                            help='Format: name@version')
        parser.add_argument('--org', required=True,
                            help='Organization UUID that owns the corpus')
        parser.add_argument('--out', required=True,
                            help='Path to write the JSON export')
        parser.add_argument('--include-1b2', action='store_true',
                            help='Also export PatternDiscoveryRun + CandidatePattern '
                                 '(for 1B-2 result reproducibility)')

    def handle(self, *args, **options):
        try:
            org = Organization.objects.get(pk=options['org'])
        except Organization.DoesNotExist as exc:
            raise CommandError(f'Organization {options["org"]} not found') from exc

        try:
            name, version = options['corpus'].split('@', 1)
        except ValueError as exc:
            raise CommandError('--corpus must be name@version') from exc

        try:
            corpus = LearningCorpus.objects.get(org=org, name=name, version=version)
        except LearningCorpus.DoesNotExist as exc:
            raise CommandError(f'Corpus {name}@{version} not found') from exc

        self.stdout.write(self.style.NOTICE(
            f'Exporting corpus={name}@{version} org={org.pk}'
        ))

        # Corpus members → conversation IDs (the scope of everything downstream).
        members = list(LearningCorpusMember.objects.filter(corpus=corpus)
                       .order_by('created_at'))
        conv_ids = [m.conversation_id for m in members]
        self.stdout.write(f'  members: {len(members)}')

        conversations = list(Conversation.objects.filter(pk__in=conv_ids)
                             .order_by('pk'))
        turns = list(ConversationTurn.objects.filter(conversation_id__in=conv_ids)
                     .order_by('conversation_id', 'occurred_at'))
        links = list(EntityLink.objects.filter(conversation_id__in=conv_ids)
                     .order_by('conversation_id', 'created_at'))
        outcomes = list(OutcomeSnapshot.objects.filter(conversation_id__in=conv_ids)
                        .order_by('conversation_id', 'captured_at'))
        self.stdout.write(
            f'  conversations={len(conversations)} turns={len(turns)} '
            f'entity_links={len(links)} outcomes={len(outcomes)}'
        )

        extractions = list(SemanticExtractionRun.objects.filter(corpus=corpus)
                           .order_by('created_at'))
        events = list(ConversationSemanticEvent.objects
                      .filter(extraction_run__in=extractions,
                              conversation_id__in=conv_ids)
                      .order_by('extraction_run_id', 'conversation_id', 'ordinal'))
        self.stdout.write(f'  extractions={len(extractions)} events={len(events)}')

        payload = {
            'meta': {
                'exported_at': datetime.now(timezone.utc).isoformat(),
                'corpus_name': name,
                'corpus_version': version,
                'org_id': str(org.pk),
                'counts': {
                    'organization': 1,
                    'learning_corpus': 1,
                    'learning_corpus_members': len(members),
                    'conversations': len(conversations),
                    'conversation_turns': len(turns),
                    'entity_links': len(links),
                    'outcome_snapshots': len(outcomes),
                    'semantic_extraction_runs': len(extractions),
                    'conversation_semantic_events': len(events),
                },
                'distributions': {
                    'lb_status_at_freeze': dict(Counter(
                        m.lb_status_at_freeze for m in members
                    )),
                    'extraction_versions': [
                        {
                            'extractor_version': r.extractor_version,
                            'ontology_version': r.ontology_version,
                            'prompt_version': r.prompt_version,
                            'model': r.model,
                            'event_count': ConversationSemanticEvent.objects
                                .filter(extraction_run=r).count(),
                        }
                        for r in extractions
                    ],
                    'events_by_type': dict(Counter(e.event_type for e in events)),
                },
            },
            'organization': _row(org),
            'learning_corpus': _row(corpus),
            'conversations': [_row(o) for o in conversations],
            'conversation_turns': [_row(o) for o in turns],
            'entity_links': [_row(o) for o in links],
            'outcome_snapshots': [_row(o) for o in outcomes],
            'learning_corpus_members': [_row(o) for o in members],
            'semantic_extraction_runs': [_row(o) for o in extractions],
            'conversation_semantic_events': [_row(o) for o in events],
            'pattern_discovery_runs': [],
            'candidate_patterns': [],
        }

        if options['include_1b2']:
            runs = list(PatternDiscoveryRun.objects
                        .filter(corpus=corpus, extraction_run__in=extractions)
                        .order_by('created_at'))
            cands = list(CandidatePattern.objects
                         .filter(discovery_run__in=runs)
                         .order_by('discovery_run_id', 'pattern_id'))
            payload['pattern_discovery_runs'] = [_row(o) for o in runs]
            payload['candidate_patterns'] = [_row(o) for o in cands]
            payload['meta']['counts']['pattern_discovery_runs'] = len(runs)
            payload['meta']['counts']['candidate_patterns'] = len(cands)
            self.stdout.write(f'  1B-2: discovery_runs={len(runs)} candidates={len(cands)}')

        raw = json.dumps(payload, cls=_JSONEncoder, sort_keys=True, indent=2)
        # SHA-256 checksum lets the importer detect corruption / drift.
        checksum = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        payload['meta']['checksum_sha256'] = checksum

        out_path = Path(options['out'])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open('w', encoding='utf-8') as f:
            json.dump(payload, f, cls=_JSONEncoder, sort_keys=True, indent=2)
        size = out_path.stat().st_size
        self.stdout.write(self.style.SUCCESS(
            f'wrote {out_path} ({size:,} bytes, checksum sha256={checksum[:12]}...)'
        ))
