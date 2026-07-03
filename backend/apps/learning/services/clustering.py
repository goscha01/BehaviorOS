"""Recommendation clustering — the Step 4 pivot.

We do NOT cluster evidence. We cluster the candidate recommendations that
analyzers extract from evidence. Different conversations can produce the
same underlying improvement idea; different improvements can come from
the same conversation. The unit of aggregation is the recommendation.

Pipeline for one clustering run:

  1. MERGE PASS
     For each unclustered candidate, compute Jaccard vs every active
     LearningSuggestion (pending/approved/watchlist) with the same
     category. If the max overlap >= MERGE_THRESHOLD, attach the
     candidate to that suggestion and refresh its aggregates.

  2. CLUSTER PASS
     For the remaining unclustered candidates, do union-find pairwise
     Jaccard within each (org, category) bucket. Connected components
     with >= LEARNING_MIN_SUPPORTING_EVIDENCE candidates become
     "potential suggestions" and get handed to the synthesizer.
     Smaller components stay unclustered (watchlist) — they'll try
     again next run when more similar candidates arrive.

  3. REJECTION CHECK (inside synthesis)
     Before creating a LearningSuggestion, the caller compares the
     cluster fingerprint against RejectedSuggestionSignature entries.
     Matches are silently dropped so previously-rejected patterns
     don't re-surface every night.

Complexity is O(N × M) for merge and O(N²) for cluster within a bucket.
That's fine at Phase 1 volumes and buys us the "same recommendation
across different phrasings" behavior without embeddings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Organization
from apps.learning.models import (
    CandidateRecommendation,
    LearningSuggestion,
    RejectedSuggestionSignature,
)
from apps.learning.services.fingerprint import jaccard, union_fingerprint

logger = logging.getLogger(__name__)


# Candidates whose top match with an existing suggestion clears this
# threshold get merged into that suggestion instead of forming a new
# cluster. Tuned conservatively — false merges are worse than a few
# duplicated suggestions we can re-cluster later.
MERGE_THRESHOLD = 0.5

# Candidates whose pairwise Jaccard clears this join into the same
# new cluster during the union-find pass. Lower than MERGE_THRESHOLD
# because we're grouping fresh candidates that already look related.
CLUSTER_THRESHOLD = 0.4

# A cluster fingerprint whose Jaccard vs a rejected signature clears
# this threshold gets suppressed for the 90-day TTL window.
REJECT_THRESHOLD = 0.5


@dataclass
class ClusteringResult:
    merged: int = 0
    clustered_new: int = 0
    watchlisted: int = 0
    rejected_suppressed: int = 0
    potential_clusters: list['CandidateCluster'] = field(default_factory=list)


@dataclass
class CandidateCluster:
    """A group of candidates that look like the same improvement idea.

    Handed to the synthesizer to become a LearningSuggestion (or dropped
    if it matches a rejected signature).
    """
    category: str
    candidates: list[CandidateRecommendation]
    fingerprint: str
    tokens: list[str]


class EvidenceClusteringService:
    """Groups CandidateRecommendation rows into LearningSuggestion clusters.

    Does not call the LLM itself — that's the synthesizer's job. This
    service returns the potential clusters; the synthesizer decides
    which become suggestions.
    """

    def __init__(self, org: Organization):
        self.org = org
        self.min_supporting = int(
            getattr(settings, 'LEARNING_MIN_SUPPORTING_EVIDENCE', 3)
        )

    def run(self) -> ClusteringResult:
        """Merge into existing suggestions, then group leftovers into
        potential new clusters. Returns clusters the synthesizer should
        turn into LearningSuggestions."""
        result = ClusteringResult()

        unclustered = list(
            CandidateRecommendation.objects
            .select_related('evidence')
            .filter(org=self.org, suggestion__isnull=True, clustered_at__isnull=True)
            .order_by('created_at')
        )
        if not unclustered:
            return result

        # Group by category — we cluster within a category bucket. Cross-
        # category recommendations don't share meaning even if the
        # wording overlaps.
        by_category: dict[str, list[CandidateRecommendation]] = {}
        for cand in unclustered:
            by_category.setdefault(cand.category, []).append(cand)

        for category, candidates in by_category.items():
            leftovers = self._merge_pass(category, candidates, result)
            self._cluster_pass(category, leftovers, result)

        return result

    def _merge_pass(
        self,
        category: str,
        candidates: list[CandidateRecommendation],
        result: ClusteringResult,
    ) -> list[CandidateRecommendation]:
        """Attach candidates to existing active suggestions when Jaccard
        clears MERGE_THRESHOLD. Returns candidates that didn't merge."""
        active = list(
            LearningSuggestion.objects
            .filter(
                org=self.org,
                category=category,
                status__in=LearningSuggestion.ACTIVE_STATUSES,
            )
            .only('id', 'fingerprint', 'representative_examples', 'supporting_count')
        )

        # Pre-tokenize existing suggestion fingerprints for cheap Jaccard.
        active_tokens: dict[str, list[str]] = {
            str(s.id): (s.fingerprint or '').split()
            for s in active
        }

        leftovers: list[CandidateRecommendation] = []
        for cand in candidates:
            best_id: str | None = None
            best_score = 0.0
            for suggestion_id, tokens in active_tokens.items():
                score = jaccard(cand.tokens or [], tokens)
                if score > best_score:
                    best_score = score
                    best_id = suggestion_id
            if best_id and best_score >= MERGE_THRESHOLD:
                self._attach(cand, best_id, similarity=best_score)
                result.merged += 1
            else:
                leftovers.append(cand)
        return leftovers

    def _cluster_pass(
        self,
        category: str,
        candidates: list[CandidateRecommendation],
        result: ClusteringResult,
    ) -> None:
        """Union-find on pairwise Jaccard >= CLUSTER_THRESHOLD.

        Components with >= min_supporting become potential clusters
        for the synthesizer. Smaller components stay unclustered so
        they can grow on future runs (watchlist behavior — no data
        loss, just deferred promotion).
        """
        n = len(candidates)
        if n == 0:
            return

        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[ri] = rj

        for i in range(n):
            for j in range(i + 1, n):
                if jaccard(candidates[i].tokens or [], candidates[j].tokens or []) >= CLUSTER_THRESHOLD:
                    union(i, j)

        components: dict[int, list[CandidateRecommendation]] = {}
        for idx, cand in enumerate(candidates):
            components.setdefault(find(idx), []).append(cand)

        for members in components.values():
            if len(members) < self.min_supporting:
                # Below promotion threshold — leave unclustered. Next run
                # picks them up alongside new candidates.
                result.watchlisted += len(members)
                continue

            fp, tokens = union_fingerprint(
                f'{c.title}\n{c.description}' for c in members
            )
            cluster = CandidateCluster(
                category=category,
                candidates=members,
                fingerprint=fp,
                tokens=tokens,
            )

            if self._matches_rejected(cluster):
                # Rejected pattern — suppress silently. Mark clustered so
                # the loop doesn't reconsider next run (until the
                # signature TTL expires and it can grow fresh candidates).
                self._suppress(members)
                result.rejected_suppressed += len(members)
                continue

            result.potential_clusters.append(cluster)

    def _matches_rejected(self, cluster: CandidateCluster) -> bool:
        signatures = RejectedSuggestionSignature.objects.filter(
            org=self.org,
            category=cluster.category,
            expires_at__gt=timezone.now(),
        ).only('tokens', 'signature')
        for sig in signatures:
            reject_tokens = sig.tokens or (sig.signature or '').split()
            if jaccard(cluster.tokens, reject_tokens) >= REJECT_THRESHOLD:
                return True
        return False

    @transaction.atomic
    def _attach(self, cand: CandidateRecommendation, suggestion_id: str, similarity: float) -> None:
        cand.suggestion_id = suggestion_id
        cand.clustered_at = timezone.now()
        cand.save(update_fields=['suggestion', 'clustered_at', 'updated_at'])

    @transaction.atomic
    def _suppress(self, candidates: Iterable[CandidateRecommendation]) -> None:
        now = timezone.now()
        ids = [c.id for c in candidates]
        CandidateRecommendation.objects.filter(id__in=ids).update(
            clustered_at=now,
            updated_at=now,
        )
