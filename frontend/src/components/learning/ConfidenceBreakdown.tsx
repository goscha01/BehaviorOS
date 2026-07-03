import type { ConfidenceBreakdown as Breakdown } from "@/lib/learning";

export function ConfidenceBreakdown({ breakdown }: { breakdown: Breakdown }) {
  const finalPct = Math.round(breakdown.final * 100);
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5">
      <div className="flex items-baseline gap-3">
        <div className="text-3xl font-bold text-gray-900">{finalPct}%</div>
        <div className="text-sm font-medium text-gray-500">confidence</div>
      </div>
      <dl className="mt-4 space-y-2 text-sm">
        <Row label="LLM (candidates)" value={breakdown.llm.toFixed(2)} />
        <Row label="LLM (synthesis)" value={breakdown.llm_synthesis.toFixed(2)} />
        <Row
          label="Support"
          value={`${breakdown.support.toFixed(2)} (${breakdown.support_count} candidates)`}
        />
        <Row
          label="Outcome consistency"
          value={breakdown.outcome_consistency.toFixed(2)}
        />
      </dl>
      <p className="mt-3 text-xs text-gray-500">
        Final = LLM (synthesis) × Support × Outcome consistency
      </p>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-gray-600">{label}</dt>
      <dd className="font-medium text-gray-900">{value}</dd>
    </div>
  );
}
