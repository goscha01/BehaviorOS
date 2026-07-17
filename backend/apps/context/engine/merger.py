"""ContextMerger — the only component that produces the final context package.

Design decisions:

- **Priority ordering**: sources are sorted ASCENDING by priority before
  merging. That means higher-priority sources run LAST and win on conflict
  (Python dict.update semantics). "Priority 100" is louder than "priority 0."
  A source may override its class-default `priority` per-call by returning
  a `SourceOutput` with a different priority — the merger honors that.

- **Provenance attached at merge time**, not by the Source. Sources return
  raw values; the merger wraps each with `{value, source, confidence,
  generated_at}`. Warning items get provenance folded in as reserved
  `_source` / `_confidence` / `_generated_at` keys — flat list items are
  easier to reason about than wrapped-dict list items.

- **Merger never raises.** A misbehaving Source is contained: exceptions
  from `provide()` are caught, recorded on the corresponding SourceResult,
  and the merge continues with the remaining Sources. If ALL Sources fail
  we still return a clean empty MergedContext.

- **Confidence math**: overall confidence = per-Source confidence weighted
  by the number of wire slots each Source populated. A Source that filled
  three slots pulls the average toward its own confidence more than one
  that filled one slot. Matches Phase 1 behavior — the wire response
  shape and confidence numbers don't change.
"""

from __future__ import annotations

import time

from django.utils import timezone

from apps.context.engine.base import (
    BUSINESS_INSIGHTS,
    CONVERSATION_HINTS,
    CUSTOMER_PROFILE,
    ContextRequest,
    MergedContext,
    RECOMMENDED_STRATEGY,
    SourceOutput,
    SourceResult,
    ProvenanceEntry,
)


def _wrap_with_provenance(value, provenance: ProvenanceEntry) -> dict:
    return {
        'value': value,
        **provenance.to_dict(),
    }


def _annotate_list_item(item: dict, provenance: ProvenanceEntry) -> dict:
    """Fold provenance into a warning / hint dict as reserved keys.

    Uses `_` prefix so the wire projection can strip them without a
    per-slot allowlist. A Source that already emitted `_source` on an
    item gets overwritten — Sources should not set reserved keys.
    """
    out = dict(item) if isinstance(item, dict) else {'value': item}
    out['_source'] = provenance.source
    out['_confidence'] = provenance.confidence
    out['_generated_at'] = provenance.generated_at.isoformat()
    return out


class ContextMerger:
    """Runs all registered Sources against a request, merges outputs into a
    single provenance-carrying MergedContext.

    Public surface is `run(request, sources_iter)` — returns a tuple of
    (MergedContext, list[SourceResult], overall_confidence).
    """

    def run(
        self,
        request: ContextRequest,
        sources_iter,
    ) -> tuple[MergedContext, list[SourceResult], float]:
        # Step 1: execute every Source, collect outputs + diagnostics.
        outputs: list[tuple[SourceOutput, SourceResult]] = []
        for name, source_cls in sources_iter:
            source = source_cls()
            started = time.perf_counter()
            error = ''
            try:
                output = source.provide(request)
                if not isinstance(output, SourceOutput):
                    # Belt AND suspenders — if a Source returns something
                    # else, treat as empty. This can happen if a Source
                    # is mid-migration to the new contract.
                    output = SourceOutput(
                        source=name,
                        priority=getattr(source, 'priority', 50),
                        confidence=0.0,
                    )
            except Exception as exc:
                output = SourceOutput(
                    source=name,
                    priority=getattr(source, 'priority', 50),
                    confidence=0.0,
                )
                error = f'{type(exc).__name__}: {exc}'
            latency_ms = int((time.perf_counter() - started) * 1000)

            result = SourceResult(
                source=name,
                priority=output.priority,
                confidence=float(output.confidence),
                contributed=not output.is_empty() and not error,
                latency_ms=latency_ms,
                error=error,
            )
            outputs.append((output, result))

        # Step 2: sort by priority ascending — higher priority runs last
        # and wins on conflict during dict merge.
        ordered = sorted(outputs, key=lambda pair: pair[0].priority)

        # Step 3: fold each output into the growing MergedContext.
        merged = MergedContext()
        for output, result in ordered:
            if result.error or output.is_empty():
                continue
            provenance = ProvenanceEntry(
                source=output.source,
                confidence=float(output.confidence),
                generated_at=output.generated_at or timezone.now(),
            )
            self._apply(merged, output, provenance)

        source_results = [r for _o, r in outputs]
        overall_confidence = self._weighted_confidence(outputs)
        return merged, source_results, overall_confidence

    # --- Slot application -------------------------------------------------

    def _apply(
        self,
        merged: MergedContext,
        output: SourceOutput,
        provenance: ProvenanceEntry,
    ) -> None:
        # Dict-shaped fact slots — wrap each entry with provenance.
        for slot in (CUSTOMER_PROFILE, BUSINESS_INSIGHTS):
            slot_facts = output.facts.get(slot) or {}
            if not slot_facts:
                continue
            target = merged.facts.setdefault(slot, {})
            for fact_name, value in slot_facts.items():
                target[fact_name] = _wrap_with_provenance(value, provenance)

        # List-shaped conversation hints — annotate each item.
        hints = output.facts.get(CONVERSATION_HINTS) or []
        if hints:
            target_hints = merged.facts.setdefault(CONVERSATION_HINTS, [])
            for item in hints:
                target_hints.append(_annotate_list_item(item, provenance))

        # Dict-shaped recommended strategy — same wrapping as fact slots.
        rec_slot = output.recommendations.get(RECOMMENDED_STRATEGY) or {}
        if rec_slot:
            target = merged.recommendations.setdefault(RECOMMENDED_STRATEGY, {})
            for rec_name, value in rec_slot.items():
                target[rec_name] = _wrap_with_provenance(value, provenance)

        # Warnings — flat list with per-item provenance folded in.
        if output.warnings:
            for item in output.warnings:
                merged.warnings.append(_annotate_list_item(item, provenance))

    # --- Confidence math --------------------------------------------------

    def _weighted_confidence(
        self,
        outputs: list[tuple[SourceOutput, SourceResult]],
    ) -> float:
        """Overall confidence = slot-count-weighted average of contributing sources.

        Matches Phase 1's slot-weighted formula so downstream monitoring
        (which is calibrated to Phase 1 numbers) doesn't have to relearn
        the distribution.
        """
        contributing = [
            (output, result) for output, result in outputs
            if result.contributed and result.confidence > 0
        ]
        if not contributing:
            return 0.0
        total_weight = 0.0
        weighted = 0.0
        for output, result in contributing:
            weight = 0.0
            if output.facts.get(CUSTOMER_PROFILE):
                weight += 1
            if output.facts.get(BUSINESS_INSIGHTS):
                weight += 1
            if output.facts.get(CONVERSATION_HINTS):
                weight += 1
            if output.recommendations.get(RECOMMENDED_STRATEGY):
                weight += 1
            if output.warnings:
                weight += 1
            weight = max(weight, 1.0)
            total_weight += weight
            weighted += weight * result.confidence
        return round(weighted / total_weight, 3) if total_weight else 0.0
