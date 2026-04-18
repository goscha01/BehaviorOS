"use client";

import { useEffect, useState, useRef, use } from "react";
import Link from "next/link";
import { apiGet } from "@/lib/api";
import type { TrainingSession } from "@/lib/types";

export default function SessionResultPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [session, setSession] = useState<TrainingSession | null>(null);
  const [loading, setLoading] = useState(true);
  const audioRef = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    apiGet<TrainingSession>(`/api/training/sessions/${id}/`)
      .then(setSession)
      .finally(() => setLoading(false));
  }, [id]);

  const playAudio = (audioUrl: string) => {
    if (audioRef.current && audioUrl) {
      const apiBase = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      audioRef.current.src = `${apiBase}/${audioUrl}`;
      audioRef.current.play().catch(() => {});
    }
  };

  if (loading) return <div className="animate-pulse h-96 bg-gray-100 rounded-lg" />;
  if (!session) return <p>Session not found</p>;

  const result = session.result;
  const signals = result?.signals?.signals || {};
  const flags = result?.signals?.flags || [];

  const outcomeColors = {
    pass: "bg-green-100 text-green-800 border-green-200",
    review: "bg-yellow-100 text-yellow-800 border-yellow-200",
    fail: "bg-red-100 text-red-800 border-red-200",
  };

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Session Result</h1>
          <p className="text-sm text-gray-500">
            {session.scenario_name} &bull; {session.business_profile_name}
          </p>
        </div>
        <Link
          href="/training"
          className="px-4 py-2 border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50"
        >
          Back to Sessions
        </Link>
      </div>

      <audio ref={audioRef} className="hidden" />

      {/* Outcome */}
      {result ? (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {/* Outcome Card */}
          <div className={`rounded-lg border-2 p-6 text-center ${outcomeColors[result.outcome]}`}>
            <p className="text-sm font-medium uppercase">Outcome</p>
            <p className="text-3xl font-bold mt-2 capitalize">{result.outcome}</p>
          </div>

          {/* Signals */}
          <div className="bg-white rounded-lg shadow p-6 md:col-span-2">
            <h3 className="text-sm font-medium text-gray-500 mb-3">Signals</h3>
            <div className="grid grid-cols-2 gap-3">
              {Object.entries(signals).map(([key, value]) => (
                <div key={key} className="flex items-center gap-2">
                  <span
                    className={`w-5 h-5 rounded-full flex items-center justify-center text-xs ${
                      value
                        ? "bg-green-100 text-green-600"
                        : "bg-red-100 text-red-600"
                    }`}
                  >
                    {value ? "\u2713" : "\u2717"}
                  </span>
                  <span className="text-sm text-gray-700">
                    {key.replace(/_/g, " ")}
                  </span>
                </div>
              ))}
            </div>

            {flags.length > 0 && (
              <div className="mt-4">
                <h4 className="text-sm font-medium text-gray-500 mb-2">Flags</h4>
                <div className="flex flex-wrap gap-2">
                  {flags.map((flag, i) => (
                    <span
                      key={i}
                      className="px-2 py-1 bg-orange-100 text-orange-800 text-xs rounded-full"
                    >
                      {flag}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Notes */}
          {result.notes && (
            <div className="bg-white rounded-lg shadow p-6 md:col-span-3">
              <h3 className="text-sm font-medium text-gray-500 mb-2">Evaluator Notes</h3>
              <p className="text-sm text-gray-700">{result.notes}</p>
            </div>
          )}
        </div>
      ) : (
        <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 px-4 py-3 rounded">
          Result is being processed. Please refresh in a moment.
        </div>
      )}

      {/* Transcript */}
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Full Transcript</h3>
        <div className="space-y-4">
          {session.turns.map((turn) => (
            <div key={turn.id} className="flex gap-3">
              <div
                className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-medium flex-shrink-0 ${
                  turn.speaker === "ai"
                    ? "bg-gray-200 text-gray-700"
                    : "bg-indigo-100 text-indigo-700"
                }`}
              >
                {turn.speaker === "ai" ? "AI" : "D"}
              </div>
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-900">
                    {turn.speaker === "ai" ? "AI Customer" : "Dispatcher"}
                  </span>
                  <span className="text-xs text-gray-400">
                    {new Date(turn.created_at).toLocaleTimeString()}
                  </span>
                  {turn.speaker === "ai" && turn.audio_url && (
                    <button
                      onClick={() => playAudio(turn.audio_url)}
                      className="text-xs text-indigo-600 hover:text-indigo-500"
                    >
                      &#9654; Play audio
                    </button>
                  )}
                </div>
                <p className="mt-1 text-sm text-gray-700 whitespace-pre-wrap">
                  {turn.text}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
