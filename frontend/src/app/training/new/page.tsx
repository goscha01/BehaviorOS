"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPost } from "@/lib/api";
import type {
  BusinessProfile,
  ScenarioTemplate,
  Script,
  TrainingSession,
  PaginatedResponse,
} from "@/lib/types";

export default function NewTrainingPage() {
  const router = useRouter();
  const [profiles, setProfiles] = useState<BusinessProfile[]>([]);
  const [scenarios, setScenarios] = useState<ScenarioTemplate[]>([]);
  const [scripts, setScripts] = useState<Script[]>([]);
  const [form, setForm] = useState({
    business_profile: "",
    scenario_template: "",
    script: "",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([
      apiGet<PaginatedResponse<BusinessProfile>>("/api/training/business-profiles/"),
      apiGet<PaginatedResponse<ScenarioTemplate>>("/api/training/scenarios/"),
      apiGet<PaginatedResponse<Script>>("/api/training/scripts/"),
    ]).then(([p, s, sc]) => {
      setProfiles(p.results);
      setScenarios(s.results);
      setScripts(sc.results);
      if (p.results.length) setForm((f) => ({ ...f, business_profile: p.results[0].id }));
      const defaultScenario = s.results.find((x) => x.is_default) || s.results[0];
      if (defaultScenario) setForm((f) => ({ ...f, scenario_template: defaultScenario.id }));
      if (sc.results.length) setForm((f) => ({ ...f, script: sc.results[0].id }));
    });
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const session = await apiPost<TrainingSession>("/api/training/sessions/", form);
      router.push(`/training/${session.id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create session");
    } finally {
      setLoading(false);
    }
  };

  const hasData = profiles.length > 0 && scenarios.length > 0 && scripts.length > 0;

  return (
    <div className="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">New Training Session</h1>
        <p className="mt-1 text-gray-500">Configure and start a dispatcher training session</p>
      </div>

      {!hasData && (
        <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 px-4 py-3 rounded">
          You need at least one business profile, scenario, and script to start a session.
          Visit Settings to create them.
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 space-y-6">
        <div>
          <label className="block text-sm font-medium text-gray-700">Business Profile</label>
          <select
            value={form.business_profile}
            onChange={(e) => setForm((f) => ({ ...f, business_profile: e.target.value }))}
            required
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
          >
            <option value="">Select a profile...</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700">Scenario Template</label>
          <select
            value={form.scenario_template}
            onChange={(e) => setForm((f) => ({ ...f, scenario_template: e.target.value }))}
            required
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
          >
            <option value="">Select a scenario...</option>
            {scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.difficulty})
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-700">Training Script</label>
          <select
            value={form.script}
            onChange={(e) => setForm((f) => ({ ...f, script: e.target.value }))}
            required
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
          >
            <option value="">Select a script...</option>
            {scripts.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} (v{s.version})
              </option>
            ))}
          </select>
        </div>

        <button
          type="submit"
          disabled={loading || !hasData}
          className="w-full py-2 px-4 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
        >
          {loading ? "Creating..." : "Create & Start Session"}
        </button>
      </form>
    </div>
  );
}
