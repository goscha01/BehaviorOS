import { BriefSuggestionRow } from "./BriefSuggestionRow";
import type { BriefSuggestion } from "@/lib/learning";

export function TrendsPanel({
  title,
  emptyLabel,
  items,
  tone,
}: {
  title: string;
  emptyLabel: string;
  items: BriefSuggestion[];
  tone?: "issue" | "opportunity";
}) {
  const accent =
    tone === "issue"
      ? "border-rose-200 bg-rose-50/50"
      : tone === "opportunity"
      ? "border-emerald-200 bg-emerald-50/50"
      : "border-gray-200 bg-white";
  return (
    <section className={`rounded-lg border p-4 ${accent}`}>
      <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
      {items.length === 0 ? (
        <p className="mt-3 text-sm text-gray-500">{emptyLabel}</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {items.map((s) => (
            <li key={s.id}>
              <BriefSuggestionRow suggestion={s} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
