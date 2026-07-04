import Link from "next/link";
import { CATEGORY_LABEL, type SuggestionCategory } from "@/lib/learning";

const ALL_CATEGORIES: SuggestionCategory[] = [
  "pricing",
  "faq",
  "qualification",
  "playbook",
  "missing_info",
  "tone",
  "other",
];

export function CategoryPills({
  counts,
}: {
  counts: Partial<Record<SuggestionCategory, number>>;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {ALL_CATEGORIES.map((cat) => {
        const n = counts[cat] ?? 0;
        return (
          <Link
            key={cat}
            href={`/dashboard/learning/queue?category=${cat}`}
            className={`rounded-full border px-3 py-1 text-sm ${
              n > 0
                ? "border-indigo-200 bg-indigo-50 text-indigo-800 hover:bg-indigo-100"
                : "border-gray-200 bg-white text-gray-500 hover:bg-gray-50"
            }`}
          >
            <span className="font-medium">{CATEGORY_LABEL[cat]}</span>
            <span className="ml-1 tabular-nums">{n}</span>
          </Link>
        );
      })}
    </div>
  );
}
