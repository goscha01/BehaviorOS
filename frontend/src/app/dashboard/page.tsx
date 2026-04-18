"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth";
import { apiGet } from "@/lib/api";
import type { Subscription, TrainingSessionListItem, PaginatedResponse } from "@/lib/types";

export default function DashboardPage() {
  const { org } = useAuth();
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [recentSessions, setRecentSessions] = useState<TrainingSessionListItem[]>([]);

  useEffect(() => {
    apiGet<Subscription>("/api/billing/subscription/").then(setSubscription).catch(() => {});
    apiGet<PaginatedResponse<TrainingSessionListItem>>("/api/training/sessions/")
      .then((data) => setRecentSessions(data.results.slice(0, 5)))
      .catch(() => {});
  }, []);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
        <p className="mt-1 text-gray-500">Welcome to {org?.name}</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Subscription Card */}
        <div className="bg-white rounded-lg shadow p-6">
          <h3 className="text-sm font-medium text-gray-500">Subscription</h3>
          {subscription?.plan ? (
            <div className="mt-2">
              <p className="text-2xl font-bold text-gray-900 capitalize">
                {subscription.plan}
              </p>
              <span
                className={`inline-block mt-1 px-2 py-0.5 text-xs rounded-full ${
                  subscription.status === "active"
                    ? "bg-green-100 text-green-800"
                    : "bg-yellow-100 text-yellow-800"
                }`}
              >
                {subscription.status}
              </span>
            </div>
          ) : (
            <div className="mt-2">
              <p className="text-gray-400">No active plan</p>
              <Link
                href="/billing"
                className="mt-2 inline-block text-sm text-indigo-600 hover:text-indigo-500"
              >
                Subscribe now
              </Link>
            </div>
          )}
        </div>

        {/* Quick Actions */}
        <div className="bg-white rounded-lg shadow p-6">
          <h3 className="text-sm font-medium text-gray-500">Quick Actions</h3>
          <div className="mt-4 space-y-3">
            <Link
              href="/training/new"
              className="block text-sm text-indigo-600 hover:text-indigo-500 font-medium"
            >
              Start New Training Session
            </Link>
            <Link
              href="/settings/business-profile"
              className="block text-sm text-indigo-600 hover:text-indigo-500 font-medium"
            >
              Manage Business Profile
            </Link>
            <Link
              href="/settings/scenarios"
              className="block text-sm text-indigo-600 hover:text-indigo-500 font-medium"
            >
              Manage Scenarios
            </Link>
          </div>
        </div>

        {/* Recent Sessions */}
        <div className="bg-white rounded-lg shadow p-6">
          <h3 className="text-sm font-medium text-gray-500">Recent Sessions</h3>
          {recentSessions.length > 0 ? (
            <ul className="mt-4 space-y-2">
              {recentSessions.map((s) => (
                <li key={s.id}>
                  <Link
                    href={
                      s.status === "completed"
                        ? `/training/${s.id}/result`
                        : `/training/${s.id}`
                    }
                    className="text-sm text-gray-700 hover:text-indigo-600"
                  >
                    {s.scenario_name || "Session"} -{" "}
                    <span className="capitalize">{s.status}</span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-4 text-sm text-gray-400">No sessions yet</p>
          )}
        </div>
      </div>
    </div>
  );
}
