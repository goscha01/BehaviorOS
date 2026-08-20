"""Regression evaluator for extractor-v3.

Runs the ontology-v3 / prompt-v3 extractor prompt against the 8
audit-derived eval fixtures in `semantic/eval_fixtures.py` and reports
per-case pass / fail. Optionally also runs the same cases through the
v2 prompt (via commit-history if desired; here we mount the v2 prompt
inline as a reference string) for a v2→v3 confusion comparison.

Does NOT touch the database. Renders each case's turns directly into
prompt form and calls the LLM once per case.

Usage:
    python manage.py evaluate_extractor_v3 \\
        [--model gpt-4o-mini] [--compare-v2] [--verbose]

Exits with code 0 if v3 passes all cases; exits 1 (via CommandError)
if any case fails so a caller can gate the deploy on this.
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from apps.conversations.semantic.eval_fixtures import (
    AUDIT_EVAL_CASES, EvalTurn, ExtractorEvalCase,
)
from apps.conversations.semantic.ontology import (
    AGENT_ACTION_EVENTS, ACKNOWLEDGMENT, FOLLOW_UP_GENERIC,
    FOLLOW_UP_SUBSTANTIVE, ONTOLOGY_VERSION,
)
from apps.conversations.semantic.prompt import (
    PROMPT_VERSION, SYSTEM_PROMPT, render_user_prompt,
)
from apps.conversations.semantic.validator import validate_events
from apps.conversations.semantic.preprocessing import TurnIdMap
from apps.learning.services.llm_client import LearningLLMClient


def _make_turn_id(i: int) -> str:
    """Turn id format matching preprocessing._make_turn_id for
    non-split turns."""
    return f't{i:04d}'


def _render_prompt_turns(case: ExtractorEvalCase) -> tuple[str, TurnIdMap]:
    """Render eval turns as [<turn_id>][speaker] text lines + build
    the turn_id_map the validator needs."""
    id_map = TurnIdMap()
    lines: list[str] = []
    for i, t in enumerate(case.turns):
        tid = _make_turn_id(i)
        id_map.id_to_parent[tid] = i
        body = t.text.replace('\n', ' ').replace('\r', ' ').strip()
        # If the turn text is empty, we still enumerate it (so an
        # eval that tests empty-turn handling can pass its turn_id
        # into the prompt) but we emit an explicit "(empty)" marker
        # so the LLM at least sees the shape.
        display = body if body else '(empty)'
        lines.append(f'[{tid}][{t.speaker}] {display}')
    return '\n'.join(lines), id_map


def _extract_case_events(
    case: ExtractorEvalCase, *, client, model: str, system_prompt: str,
) -> tuple[list[dict], list[dict]]:
    """Run one case through the LLM + validator. Applies the extractor-v3
    empty-text gate too — an agent event referencing a turn whose original
    text was empty/whitespace is dropped.

    Returns (accepted_events, rejected_records).
    """
    rendered, id_map = _render_prompt_turns(case)
    user_prompt = render_user_prompt(rendered)
    llm_result = client.analyze(
        system_prompt=system_prompt, user_prompt=user_prompt,
        model=model, max_tokens=1500,
    )
    validated = validate_events(llm_result.parsed_json, turn_id_map=id_map)
    # Extractor-v3 empty-text gate: drop agent-actor events whose
    # source turn text was empty/whitespace.
    empty_turn_parents = {
        i for i, t in enumerate(case.turns) if not t.text.strip()
    }
    gated = [
        ev for ev in validated.events
        if not (ev.get('actor') == 'agent'
                and ev.get('turn_start') in empty_turn_parents)
    ]
    return gated, validated.rejected


def _evaluate_one(
    case: ExtractorEvalCase, *, client, model: str, system_prompt: str,
) -> dict:
    events, rejected = _extract_case_events(
        case, client=client, model=model, system_prompt=system_prompt,
    )
    emitted_types = {ev['event_type'] for ev in events}
    missing = [t for t in case.should_emit if t not in emitted_types]
    forbidden_found = [t for t in case.must_not_emit if t in emitted_types]
    # Empty-turn check: no agent event may reference a turn in empty_turn_indices.
    empty_violations = []
    for ev in events:
        if ev.get('actor') == 'agent' and ev.get('turn_start') in case.empty_turn_indices:
            empty_violations.append(f"{ev['event_type']} @ turn {ev['turn_start']}")
    passed = not missing and not forbidden_found and not empty_violations
    return {
        'case': case.name,
        'passed': passed,
        'emitted_types': sorted(emitted_types),
        'missing': missing,
        'forbidden_found': forbidden_found,
        'empty_violations': empty_violations,
        'rejected_count': len(rejected),
    }


class Command(BaseCommand):
    help = ('Run extractor-v3 regression eval against the 8 audit-derived '
            'fixtures and report pass/fail per case.')

    def add_arguments(self, parser):
        parser.add_argument('--model', default='gpt-4o-mini')
        parser.add_argument('--verbose', action='store_true',
                            help='Print full event lists per case')

    def handle(self, *args, **options):
        client = LearningLLMClient()
        self.stdout.write(self.style.NOTICE(
            f'Extractor-v3 regression eval: {len(AUDIT_EVAL_CASES)} cases, '
            f'ontology={ONTOLOGY_VERSION} prompt={PROMPT_VERSION} '
            f'model={options["model"]}'
        ))
        results = []
        for case in AUDIT_EVAL_CASES:
            self.stdout.write(f'  [{case.name}] ... ', ending='')
            self.stdout.flush()
            try:
                res = _evaluate_one(
                    case, client=client, model=options['model'],
                    system_prompt=SYSTEM_PROMPT,
                )
            except Exception as exc:
                res = {
                    'case': case.name, 'passed': False,
                    'emitted_types': [], 'missing': [],
                    'forbidden_found': [], 'empty_violations': [],
                    'rejected_count': 0, 'error': repr(exc),
                }
            results.append(res)
            self.stdout.write(
                self.style.SUCCESS('PASS') if res['passed']
                else self.style.ERROR('FAIL')
            )
            if options['verbose'] or not res['passed']:
                if res.get('error'):
                    self.stdout.write(f'    ERROR: {res["error"]}')
                self.stdout.write(f'    emitted: {res["emitted_types"]}')
                if res['missing']:
                    self.stdout.write(f'    MISSING (should_emit): {res["missing"]}')
                if res['forbidden_found']:
                    self.stdout.write(f'    FORBIDDEN found (must_not_emit): {res["forbidden_found"]}')
                if res['empty_violations']:
                    self.stdout.write(f'    EMPTY-TURN violations: {res["empty_violations"]}')

        passed = sum(1 for r in results if r['passed'])
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Result: {passed}/{len(results)} cases pass'
        ))

        if passed != len(results):
            failed = [r['case'] for r in results if not r['passed']]
            raise CommandError(f'{len(failed)} case(s) failed: {failed}')
