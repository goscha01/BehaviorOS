import Link from "next/link";
import {
  CATEGORY_LABEL,
  statusBadgeClass,
  STATUS_LABEL,
  type BriefSuggestion,
} from "@/lib/learning";

export function BriefSuggestionRow({ suggestion }: { suggestion: BriefSuggestion }) {
  const confidencePct = Math.round(parseFloat(suggestion.confidence) * 100);
  return (
    <Link
      href={`/dashboard/learning/${suggestion.id}`}
      className="flex items-center gap-3 rounded-md border border-gray-200 bg-white p-3 hover:border-indigo-300 hover:bg-indigo-50/30"
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-xs">
          <span
            className={`rounded-full px-2 py-0.5 font-medium ${statusBadgeClass(suggestion.status)}`}
          >
            {STATUS_LABEL[suggestion.status]}
          </span>
          <span className="rounded-full bg-gray-100 px-2 py-0.5 font-medium text-gray-700">
            {CATEGORY_LABEL[suggestion.category]}
          </span>
        </div>
        <p className="mt-1 truncate text-sm font-medium text-gray-900">
          {suggestion.title}
        </p>
      </div>
      <div className="shrink-0 text-right">
        <div className="text-sm font-bold text-gray-900">{confidencePct}%</div>
        <div className="text-xs text-gray-500">{suggestion.supporting_count} supp</div>
      </div>
    </Link>
  );
}
