"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BriefSuggestionRow } from "@/components/learning/BriefSuggestionRow";
import { CategoryPills } from "@/components/learning/CategoryPills";
import { TrendsPanel } from "@/components/learning/TrendsPanel";
import { getMorningBrief, type MorningBrief } from "@/lib/learning";

export default function LearningMorningBriefPage() {
  const [brief, setBrief] = useState<MorningBrief | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getMorningBrief(30)
      .then((data) => {
        if (!cancelled) setBrief(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <p className="text-sm text-gray-500">Loading morning brief…</p>;
  if (error) return <p className="text-sm text-rose-600">{error}</p>;
  if (!brief) return <p className="text-sm text-gray-500">No data.</p>;

  const lastJobLine = brief.last_job
    ? `Last nightly run ${humanTime(brief.last_job.completed_at)} — ${
        brief.last_job.evidence_processed
      } evidence analyzed, ${brief.last_job.suggestions_created} suggestions created, $${
        brief.last_job.cost_usd
      } spent.`
    : "No nightly run has completed yet. Run manually with `manage.py run_ingestion → run_analysis → run_clustering`, or wait for the beat schedule.";

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <Link
            href="/dashboard"
            className="text-sm text-gray-500 hover:text-indigo-600"
          >
            ← Dashboard
          </Link>
          <h1 className="mt-1 text-2xl font-bold text-gray-900">
            BehaviorOS — What did we learn?
          </h1>
          <p className="mt-1 text-gray-500">{lastJobLine}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Link
            href="/dashboard/integrations"
            className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            Integrations
          </Link>
          <Link
            href="/dashboard/learning/queue"
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700"
          >
            Open review queue →
          </Link>
        </div>
      </div>

      {/* Today's suggestions */}
      <section>
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-lg font-semibold text-gray-900">
            Today&apos;s suggestions
          </h2>
          <span className="text-sm text-gray-500">
            {brief.new_suggestions_today.count} since{" "}
            {humanDate(brief.new_suggestions_today.since)}
          </span>
        </div>
        {brief.new_suggestions_today.top.length === 0 ? (
          <p className="text-sm text-gray-500">
            Nothing new. The nightly job either hasn&apos;t run or didn&apos;t find new patterns.
          </p>
        ) : (
          <ul className="space-y-2">
            {brief.new_suggestions_today.top.map((s) => (
              <li key={s.id}>
                <BriefSuggestionRow suggestion={s} />
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Categories */}
      <section>
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-lg font-semibold text-gray-900">By category</h2>
          <span className="text-sm text-gray-500">Last {brief.window_days} days</span>
        </div>
        <CategoryPills counts={brief.category_counts} />
      </section>

      {/* Trends — three columns */}
      <section>
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-lg font-semibold text-gray-900">Trends</h2>
          <span className="text-sm text-gray-500">Last {brief.window_days} days</span>
        </div>
        <div className="grid gap-4 lg:grid-cols-3">
          <TrendsPanel
            title="Top by support"
            emptyLabel="No patterns yet."
            items={brief.trends.top_supported}
          />
          <TrendsPanel
            title="Recurring issues"
            emptyLabel="No consistently-negative patterns."
            items={brief.trends.recurring_issues}
            tone="issue"
          />
          <TrendsPanel
            title="Recurring opportunities"
            emptyLabel="No consistently-positive patterns."
            items={brief.trends.recurring_opportunities}
            tone="opportunity"
          />
        </div>
      </section>
    </div>
  );
}

function humanDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function humanTime(iso: string | null): string {
  if (!iso) return "unknown time";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
