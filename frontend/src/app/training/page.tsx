"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { apiGet } from "@/lib/api";
import type { TrainingSessionListItem, PaginatedResponse } from "@/lib/types";

export default function TrainingListPage() {
  const [sessions, setSessions] = useState<TrainingSessionListItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiGet<PaginatedResponse<TrainingSessionListItem>>("/api/training/sessions/")
      .then((data) => setSessions(data.results))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const statusColor = (status: string) => {
    switch (status) {
      case "completed": return "bg-green-100 text-green-800";
      case "running": return "bg-blue-100 text-blue-800";
      case "failed": return "bg-red-100 text-red-800";
      default: return "bg-gray-100 text-gray-800";
    }
  };

  if (loading) return <div className="animate-pulse h-64 bg-gray-100 rounded-lg" />;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Training Sessions</h1>
          <p className="mt-1 text-gray-500">View and manage dispatcher training sessions</p>
        </div>
        <Link
          href="/training/new"
          className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700"
        >
          New Session
        </Link>
      </div>

      {sessions.length === 0 ? (
        <div className="text-center py-12">
          <p className="text-gray-400">No training sessions yet.</p>
          <Link
            href="/training/new"
            className="mt-4 inline-block text-indigo-600 hover:text-indigo-500 font-medium"
          >
            Start your first session
          </Link>
        </div>
      ) : (
        <div className="bg-white rounded-lg shadow overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Scenario</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Started</th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {sessions.map((s) => (
                <tr key={s.id} className="hover:bg-gray-50">
                  <td className="px-6 py-4 text-sm text-gray-900">
                    {s.scenario_name || "Unnamed scenario"}
                    {s.business_profile_name && (
                      <span className="block text-xs text-gray-500">{s.business_profile_name}</span>
                    )}
                  </td>
                  <td className="px-6 py-4">
                    <span className={`px-2 py-1 text-xs rounded-full ${statusColor(s.status)}`}>
                      {s.status}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-sm text-gray-500">
                    {s.started_at ? new Date(s.started_at).toLocaleString() : "Not started"}
                  </td>
                  <td className="px-6 py-4 text-sm">
                    {s.status === "created" && (
                      <Link href={`/training/${s.id}`} className="text-indigo-600 hover:text-indigo-500">
                        Start
                      </Link>
                    )}
                    {s.status === "running" && (
                      <Link href={`/training/${s.id}`} className="text-indigo-600 hover:text-indigo-500">
                        Continue
                      </Link>
                    )}
                    {s.status === "completed" && (
                      <Link href={`/training/${s.id}/result`} className="text-indigo-600 hover:text-indigo-500">
                        View Result
                      </Link>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
