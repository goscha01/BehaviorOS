"use client";

import { useState } from "react";
import {
  approveSuggestion,
  archiveSuggestion,
  markImplemented,
  markMeasured,
  rejectSuggestion,
  startReview,
  type SuggestionDetail,
  type SuggestionStatus,
} from "@/lib/learning";
import { RejectDialog } from "./RejectDialog";

const NEXT_ACTIONS: Record<SuggestionStatus, Array<"start_review" | "approve" | "reject" | "implement" | "measure" | "archive">> = {
  new: ["start_review", "approve", "reject", "archive"],
  under_review: ["approve", "reject", "archive"],
  approved: ["implement", "archive"],
  implemented: ["measure", "archive"],
  measured: ["archive"],
  watchlist: ["archive"],
  archived: [],
  rejected: [],
};

const LABEL: Record<string, string> = {
  start_review: "Start review",
  approve: "Approve",
  reject: "Reject",
  implement: "Mark implemented",
  measure: "Record impact",
  archive: "Archive",
};

const STYLE: Record<string, string> = {
  start_review: "border-amber-500 text-amber-700 hover:bg-amber-50",
  approve: "bg-emerald-600 text-white hover:bg-emerald-700",
  reject: "border-rose-500 text-rose-700 hover:bg-rose-50",
  implement: "bg-indigo-600 text-white hover:bg-indigo-700",
  measure: "bg-purple-600 text-white hover:bg-purple-700",
  archive: "border-gray-300 text-gray-700 hover:bg-gray-50",
};

export function LifecycleActions({
  suggestion,
  onChanged,
}: {
  suggestion: SuggestionDetail;
  onChanged: (updated: SuggestionDetail) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [rejectOpen, setRejectOpen] = useState(false);

  const actions = NEXT_ACTIONS[suggestion.status];

  async function run(action: string, fn: () => Promise<{ suggestion: SuggestionDetail }>) {
    setBusy(action);
    setError("");
    try {
      const res = await fn();
      onChanged(res.suggestion);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed.");
    } finally {
      setBusy(null);
    }
  }

  async function handleImplement() {
    const publishReceipts: Record<string, unknown> = {};
    for (const target of suggestion.publish_targets || []) {
      publishReceipts[target] = { status: "manual", note: "Marked implemented by reviewer" };
    }
    return run("implement", () => markImplemented(suggestion.id, publishReceipts));
  }

  async function handleMeasure() {
    const raw = window.prompt(
      'Impact JSON — e.g. {"win_rate_before": 0.42, "win_rate_after": 0.51, "sample_size": 320}'
    );
    if (!raw) return;
    let impact: Record<string, unknown>;
    try {
      impact = JSON.parse(raw);
    } catch {
      setError("Impact must be valid JSON.");
      return;
    }
    return run("measure", () => markMeasured(suggestion.id, impact));
  }

  if (actions.length === 0) {
    return <p className="text-sm text-gray-500">This suggestion is in a terminal state.</p>;
  }

  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {actions.map((action) => {
          const cls = STYLE[action];
          const isPrimary = cls.includes("bg-");
          const base = isPrimary
            ? "rounded-md px-4 py-2 text-sm font-medium disabled:opacity-50"
            : "rounded-md border px-4 py-2 text-sm font-medium disabled:opacity-50";
          const label = busy === action ? "Working…" : LABEL[action];

          if (action === "start_review") {
            return (
              <button
                key={action}
                type="button"
                className={`${base} ${cls}`}
                disabled={busy !== null}
                onClick={() => run("start_review", () => startReview(suggestion.id))}
              >
                {label}
              </button>
            );
          }
          if (action === "approve") {
            return (
              <button
                key={action}
                type="button"
                className={`${base} ${cls}`}
                disabled={busy !== null}
                onClick={() =>
                  run("approve", () =>
                    approveSuggestion(suggestion.id, {
                      publish_to: suggestion.publish_targets,
                    })
                  )
                }
              >
                {label}
              </button>
            );
          }
          if (action === "reject") {
            return (
              <button
                key={action}
                type="button"
                className={`${base} ${cls}`}
                disabled={busy !== null}
                onClick={() => setRejectOpen(true)}
              >
                Reject
              </button>
            );
          }
          if (action === "implement") {
            return (
              <button
                key={action}
                type="button"
                className={`${base} ${cls}`}
                disabled={busy !== null}
                onClick={handleImplement}
              >
                {label}
              </button>
            );
          }
          if (action === "measure") {
            return (
              <button
                key={action}
                type="button"
                className={`${base} ${cls}`}
                disabled={busy !== null}
                onClick={handleMeasure}
              >
                {label}
              </button>
            );
          }
          if (action === "archive") {
            return (
              <button
                key={action}
                type="button"
                className={`${base} ${cls}`}
                disabled={busy !== null}
                onClick={() => run("archive", () => archiveSuggestion(suggestion.id))}
              >
                {label}
              </button>
            );
          }
          return null;
        })}
      </div>
      {error && <p className="mt-2 text-sm text-rose-600">{error}</p>}
      <RejectDialog
        open={rejectOpen}
        onClose={() => setRejectOpen(false)}
        onSubmit={async (reason) => {
          const res = await rejectSuggestion(suggestion.id, reason);
          onChanged(res.suggestion);
        }}
      />
    </div>
  );
}
