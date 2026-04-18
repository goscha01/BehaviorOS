"use client";

import { useEffect, useState, useRef, use } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPost } from "@/lib/api";
import type { TrainingSession, SessionTurn } from "@/lib/types";

export default function TrainingSessionPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const router = useRouter();
  const [session, setSession] = useState<TrainingSession | null>(null);
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const chatEndRef = useRef<HTMLDivElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    apiGet<TrainingSession>(`/api/training/sessions/${id}/`).then(setSession);
  }, [id]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [session?.turns]);

  const handleStart = async () => {
    setSending(true);
    setError("");
    try {
      const updated = await apiPost<TrainingSession>(
        `/api/training/sessions/${id}/start/`
      );
      setSession(updated);
      playLatestAudio(updated.turns);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to start session");
    } finally {
      setSending(false);
    }
  };

  const handleSendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!message.trim() || sending) return;
    setSending(true);
    setError("");
    try {
      const updated = await apiPost<TrainingSession>(
        `/api/training/sessions/${id}/turn/`,
        { text: message }
      );
      setSession(updated);
      setMessage("");
      playLatestAudio(updated.turns);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to send message");
    } finally {
      setSending(false);
    }
  };

  const handleComplete = async () => {
    if (!confirm("End this training session?")) return;
    setSending(true);
    try {
      const updated = await apiPost<TrainingSession>(
        `/api/training/sessions/${id}/complete/`
      );
      setSession(updated);
      router.push(`/training/${id}/result`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to complete session");
    } finally {
      setSending(false);
    }
  };

  const playLatestAudio = (turns: SessionTurn[]) => {
    const lastAiTurn = [...turns].reverse().find((t) => t.speaker === "ai" && t.audio_url);
    if (lastAiTurn?.audio_url && audioRef.current) {
      const apiBase = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      audioRef.current.src = `${apiBase}/${lastAiTurn.audio_url}`;
      audioRef.current.play().catch(() => {});
    }
  };

  const playAudio = (audioUrl: string) => {
    if (audioRef.current && audioUrl) {
      const apiBase = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      audioRef.current.src = `${apiBase}/${audioUrl}`;
      audioRef.current.play().catch(() => {});
    }
  };

  if (!session) {
    return <div className="animate-pulse h-96 bg-gray-100 rounded-lg" />;
  }

  if (session.status === "completed") {
    router.push(`/training/${id}/result`);
    return null;
  }

  return (
    <div className="max-w-3xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Training Session</h1>
          <p className="text-sm text-gray-500">
            {session.scenario_name} &bull; {session.business_profile_name}
          </p>
        </div>
        {session.status === "running" && (
          <button
            onClick={handleComplete}
            disabled={sending}
            className="px-4 py-2 bg-red-600 text-white rounded-md text-sm font-medium hover:bg-red-700 disabled:opacity-50"
          >
            End Session
          </button>
        )}
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-3 py-2 rounded text-sm">
          {error}
        </div>
      )}

      <audio ref={audioRef} className="hidden" />

      {/* Chat area */}
      <div className="bg-white rounded-lg shadow h-[500px] flex flex-col">
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {session.status === "created" && (
            <div className="flex items-center justify-center h-full">
              <button
                onClick={handleStart}
                disabled={sending}
                className="px-6 py-3 bg-indigo-600 text-white rounded-lg text-lg font-medium hover:bg-indigo-700 disabled:opacity-50"
              >
                {sending ? "Starting..." : "Start Training Session"}
              </button>
            </div>
          )}

          {session.turns.map((turn) => (
            <div
              key={turn.id}
              className={`flex ${
                turn.speaker === "candidate" ? "justify-end" : "justify-start"
              }`}
            >
              <div
                className={`max-w-[75%] rounded-lg px-4 py-3 ${
                  turn.speaker === "candidate"
                    ? "bg-indigo-600 text-white"
                    : "bg-gray-100 text-gray-900"
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-medium opacity-75">
                    {turn.speaker === "ai" ? "AI Customer" : "You"}
                  </span>
                  {turn.speaker === "ai" && turn.audio_url && (
                    <button
                      onClick={() => playAudio(turn.audio_url)}
                      className="text-xs opacity-75 hover:opacity-100"
                      title="Play audio"
                    >
                      &#9654;
                    </button>
                  )}
                </div>
                <p className="text-sm whitespace-pre-wrap">{turn.text}</p>
              </div>
            </div>
          ))}
          <div ref={chatEndRef} />
        </div>

        {/* Input */}
        {session.status === "running" && (
          <form
            onSubmit={handleSendMessage}
            className="border-t border-gray-200 p-4 flex gap-3"
          >
            <input
              type="text"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder="Type your response as the dispatcher..."
              disabled={sending}
              className="flex-1 rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 disabled:opacity-50"
              autoFocus
            />
            <button
              type="submit"
              disabled={sending || !message.trim()}
              className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
            >
              {sending ? "..." : "Send"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
