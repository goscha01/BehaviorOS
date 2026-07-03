"use client";

import { useEffect, useState } from "react";
import { getSupportingEvidence, type SupportingEvidenceItem } from "@/lib/learning";

export function SupportingEvidenceList({ suggestionId }: { suggestionId: string }) {
  const [items, setItems] = useState<SupportingEvidenceItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getSupportingEvidence(suggestionId)
      .then((data) => {
        if (cancelled) return;
        setItems(data.results);
        setTotal(data.count);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [suggestionId]);

  if (loading) {
    return <p className="text-sm text-gray-500">Loading supporting evidence…</p>;
  }
  if (items.length === 0) {
    return <p className="text-sm text-gray-500">No supporting evidence yet.</p>;
  }

  return (
    <div>
      <p className="mb-3 text-sm text-gray-600">
        {total} supporting {total === 1 ? "candidate" : "candidates"} across evidence sources
      </p>
      <ul className="space-y-3">
        {items.map((item) => (
          <li key={item.id} className="rounded-lg border border-gray-200 bg-white p-4">
            <div className="flex items-center gap-2 text-xs">
              <span className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700">
                {item.source_system}
              </span>
              <span className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700">
                {item.evidence_type}
              </span>
              <span className={`rounded-full px-2 py-0.5 font-medium ${outcomeBadge(item.outcome_signal)}`}>
                {item.outcome_signal}
              </span>
              <span className="text-gray-500">
                confidence {parseFloat(item.llm_confidence).toFixed(2)}
              </span>
            </div>
            <p className="mt-2 font-medium text-gray-900">{item.title}</p>
            {item.description ? (
              <p className="mt-1 text-sm text-gray-600">{item.description}</p>
            ) : null}
            {item.evidence_summary ? (
              <p className="mt-2 text-xs italic text-gray-500">
                From: {item.evidence_summary}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function outcomeBadge(signal: string): string {
  switch (signal) {
    case "positive":
      return "bg-emerald-100 text-emerald-800";
    case "negative":
      return "bg-rose-100 text-rose-800";
    default:
      return "bg-gray-100 text-gray-700";
  }
}
