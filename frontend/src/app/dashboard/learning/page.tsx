"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { SuggestionCard } from "@/components/learning/SuggestionCard";
import {
  listSuggestions,
  type SuggestionListItem,
  type SuggestionStatus,
} from "@/lib/learning";

const TABS: Array<{ key: string; label: string; statuses: SuggestionStatus[] }> = [
  { key: "new", label: "New", statuses: ["new", "watchlist"] },
  { key: "review", label: "Under Review", statuses: ["under_review"] },
  {
    key: "approved",
    label: "Approved",
    statuses: ["approved", "implemented", "measured"],
  },
  { key: "archive", label: "Archive", statuses: ["archived", "rejected"] },
];

export default function LearningDashboardPage() {
  const [activeTab, setActiveTab] = useState(TABS[0]);
  const [items, setItems] = useState<SuggestionListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    listSuggestions({ status: activeTab.statuses })
      .then((data) => {
        if (cancelled) return;
        setItems(data.results);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to load.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeTab]);

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/dashboard"
          className="text-sm text-gray-500 hover:text-indigo-600"
        >
          ← Dashboard
        </Link>
        <h1 className="mt-1 text-2xl font-bold text-gray-900">
          Learning Review Queue
        </h1>
        <p className="mt-1 text-gray-500">
          Business improvements BehaviorOS recommends for human approval.
        </p>
      </div>

      <div className="border-b border-gray-200">
        <nav className="-mb-px flex gap-6">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => setActiveTab(tab)}
              className={`border-b-2 px-1 pb-2 text-sm font-medium ${
                activeTab.key === tab.key
                  ? "border-indigo-500 text-indigo-600"
                  : "border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : error ? (
        <p className="text-sm text-rose-600">{error}</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-gray-500">
          Nothing in {activeTab.label.toLowerCase()} yet.
        </p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {items.map((item) => (
            <SuggestionCard key={item.id} suggestion={item} />
          ))}
        </div>
      )}
    </div>
  );
}
