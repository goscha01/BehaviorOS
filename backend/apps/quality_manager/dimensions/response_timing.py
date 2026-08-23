"""Response Timing dimension — QM V1 second shipped dimension.

Minimal semantic per operator directive (2026-08-23):

  1. Read the tenant's existing response-time policy from the latest
     TenantConfigSnapshot.raw_config_json. NO defaults — absent policy
     yields UNKNOWN, never a fabricated compliance warning.
  2. Find the first customer message in the conversation.
  3. Find the first agent message that comes after it.
  4. Compute latency_seconds.
  5. Configured SLA + latency ≤ SLA → PASS.
     Configured SLA + latency > SLA → FAIL (severity by ratio).
     No configured SLA → UNKNOWN_NOT_EVALUABLE (latency still stored
     as evidence).
     No customer message OR no post-customer agent reply → NOT_APPLICABLE.
  6. Evidence attached to every non-NOT_APPLICABLE evaluation:
     the two turn IDs + timestamps + latency_seconds.

Does NOT add:
  * business-hours logic
  * multi-transition tracking (only first customer→agent pair)
  * default SLAs
  * new LB endpoints or policy tables
  * grading

Only Reads:
  * ConversationTurn (occurred_at, speaker, source_turn_id)
  * TenantConfigSnapshot.raw_config_json (candidate SLA paths)
"""

from __future__ import annotations

from typing import Iterable, Optional

from apps.quality_manager.dimensions import register
from apps.quality_manager.dimensions.base import (
    BaseDimension,
    DimensionResult,
    EvidenceRef,
    State,
)


VERSION = 'qm-v1-timing.1'


# Candidate JSON paths inside TenantConfigSnapshot.raw_config_json that
# an operator MIGHT set to express "first response should be within N
# seconds." Kept plural so we accept a few conventional names WITHOUT
# adding a new policy field to LB. If none is populated, we emit
# UNKNOWN — the "no default SLA" invariant. Add more paths ONLY when
# a real LB setting starts populating one.
_CONFIGURED_SLA_PATHS: tuple[tuple[str, ...], ...] = (
    ('user', 'first_response_sla_seconds'),
    ('user', 'response_time_sla_seconds'),
    ('response_time_policy', 'first_response_sla_seconds'),
    ('sla', 'first_response_seconds'),
)


def _read_sla_seconds(raw_config: dict | None) -> tuple[Optional[int], Optional[str]]:
    """Return (seconds, source_path) or (None, None) if no SLA configured.

    Rejects non-integer or non-positive values as "not configured" —
    an explicitly-set 0 is meaningless and treated as absent.
    """
    if not isinstance(raw_config, dict):
        return None, None
    for path in _CONFIGURED_SLA_PATHS:
        node: object = raw_config
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
            if node is None:
                break
        if node is None:
            continue
        try:
            seconds = int(node)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds, '.'.join(path)
    return None, None


def _severity_from_ratio(latency_seconds: float, sla_seconds: int) -> str:
    """Map latency/SLA ratio to info/warning/critical.

    Ratio 1.0 = exactly at SLA (PASS boundary; the caller should have
    checked already). Beyond SLA:
      ratio <= 1.5x   → info
      ratio <= 3.0x   → warning
      ratio >  3.0x   → critical
    """
    if sla_seconds <= 0:
        return 'warning'
    ratio = latency_seconds / sla_seconds
    if ratio <= 1.5:
        return 'info'
    if ratio <= 3.0:
        return 'warning'
    return 'critical'


def _format_latency(seconds: float) -> str:
    """Compact human string: e.g. '42s' / '3m 12s' / '2h 5m'."""
    if seconds < 60:
        return f'{seconds:.0f}s'
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f'{minutes}m {sec:02d}s'
    hours, m = divmod(minutes, 60)
    return f'{hours}h {m:02d}m'


@register
class ResponseTimingDimension(BaseDimension):
    name = 'response_timing'
    version = VERSION

    def evaluate(
        self, *,
        reconstruction_run,
        conversation,
    ) -> Iterable[DimensionResult]:
        from apps.conversations.models import (
            Conversation, ConversationTurn, Speaker,
            TenantConfigSnapshot,
        )
        conv_id = str(conversation.id)

        # 1. Find first customer message + first agent reply after it.
        # ConversationTurn Meta.ordering is `['occurred_at']`, so
        # iterating in DB order is chronologically correct.
        turns = list(
            ConversationTurn.objects
            .filter(conversation=conversation)
            .order_by('occurred_at')
            .only('id', 'source_turn_id', 'speaker', 'occurred_at')
        )
        first_cust = next(
            (t for t in turns if t.speaker == Speaker.CUSTOMER), None,
        )
        if first_cust is None:
            yield DimensionResult(
                dimension=self.name,
                state=State.NOT_APPLICABLE,
                conversation_id=conv_id,
                reason_code='no_customer_message',
                rationale_text='Conversation has no customer message to respond to.',
            )
            return

        first_agent_after = next(
            (
                t for t in turns
                if t.speaker == Speaker.AGENT
                and t.occurred_at is not None
                and first_cust.occurred_at is not None
                and t.occurred_at > first_cust.occurred_at
            ),
            None,
        )
        if first_agent_after is None:
            yield DimensionResult(
                dimension=self.name,
                state=State.NOT_APPLICABLE,
                conversation_id=conv_id,
                reason_code='no_agent_reply_after_customer',
                rationale_text=(
                    'Conversation has a customer message but no subsequent '
                    'agent reply to measure timing against.'
                ),
                evidence=[EvidenceRef(
                    kind='conversation_turn', ref=first_cust.source_turn_id,
                    description=(
                        f'first customer message at {first_cust.occurred_at.isoformat()}'
                    ),
                )],
            )
            return

        latency_seconds = (
            first_agent_after.occurred_at - first_cust.occurred_at
        ).total_seconds()

        # 2. Read the tenant's configured SLA (if any).
        snapshot = (
            TenantConfigSnapshot.objects
            .filter(org=conversation.org, source_system='leadbridge')
            .order_by('-created_at').first()
        )
        raw = snapshot.raw_config_json if snapshot else None
        sla_seconds, sla_path = _read_sla_seconds(raw)

        latency_str = _format_latency(latency_seconds)
        base_evidence = [
            EvidenceRef(
                kind='conversation_turn',
                ref=first_cust.source_turn_id,
                description=(
                    f'first customer message at {first_cust.occurred_at.isoformat()}'
                ),
            ),
            EvidenceRef(
                kind='conversation_turn',
                ref=first_agent_after.source_turn_id,
                description=(
                    f'first agent reply at {first_agent_after.occurred_at.isoformat()}'
                ),
            ),
            EvidenceRef(
                kind='timing_metric',
                ref=f'{latency_seconds:.3f}',
                description=(
                    f'latency={latency_str} '
                    f'({latency_seconds:.1f}s from customer to first agent reply)'
                ),
            ),
        ]

        # 3. No configured SLA → UNKNOWN (latency still recorded).
        if sla_seconds is None:
            yield DimensionResult(
                dimension=self.name,
                state=State.UNKNOWN_NOT_EVALUABLE,
                conversation_id=conv_id,
                reason_code='no_configured_response_sla',
                rationale_text=(
                    f'First-response latency was {latency_str} but no '
                    f'response-time SLA is configured on the tenant. '
                    f'Recording latency; no pass/fail judgment made.'
                ),
                evidence=base_evidence,
            )
            return

        # 4. Configured SLA → PASS or FAIL.
        base_evidence.append(EvidenceRef(
            kind='configured_rule',
            ref=sla_path or 'unknown',
            description=f'response SLA: {sla_seconds}s at raw_config.{sla_path}',
        ))
        if latency_seconds <= sla_seconds:
            yield DimensionResult(
                dimension=self.name,
                state=State.PASS,
                conversation_id=conv_id,
                reason_code='within_sla',
                rationale_text=(
                    f'First-response latency {latency_str} is within the '
                    f'configured SLA of {sla_seconds}s.'
                ),
                evidence=base_evidence,
            )
            return

        severity = _severity_from_ratio(latency_seconds, sla_seconds)
        overshoot = latency_seconds - sla_seconds
        ratio_x = latency_seconds / sla_seconds
        yield DimensionResult(
            dimension=self.name,
            state=State.FAIL,
            conversation_id=conv_id,
            severity=severity,
            reason_code='over_sla',
            rationale_text=(
                f'First-response latency {latency_str} exceeded configured '
                f'SLA of {sla_seconds}s by {overshoot:.0f}s ({ratio_x:.1f}x).'
            ),
            evidence=base_evidence,
        )

    def evaluate_corpus(self, *, reconstruction_run) -> Iterable[DimensionResult]:
        # No corpus-level pattern for Timing V1. Per-conversation only.
        return []
