import Link from "next/link";
import {
  CATEGORY_LABEL,
  STATUS_LABEL,
  statusBadgeClass,
  type SuggestionListItem,
} from "@/lib/learning";

export function SuggestionCard({ suggestion }: { suggestion: SuggestionListItem }) {
  const confidencePct = Math.round(parseFloat(suggestion.confidence) * 100);
  return (
    <Link
      href={`/dashboard/learning/${suggestion.id}`}
      className="block rounded-lg border border-gray-200 bg-white p-5 shadow-sm hover:border-indigo-300 hover:shadow"
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${statusBadgeClass(
                suggestion.status
              )}`}
            >
              {STATUS_LABEL[suggestion.status]}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700">
              {CATEGORY_LABEL[suggestion.category]}
            </span>
          </div>
          <h3 className="mt-2 text-base font-semibold text-gray-900 line-clamp-2">
            {suggestion.title}
          </h3>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-lg font-bold text-gray-900">{confidencePct}%</div>
          <div className="text-xs text-gray-500">
            {suggestion.supporting_count} supporting
          </div>
        </div>
      </div>
    </Link>
  );
}
