"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost, apiPut, apiDelete } from "@/lib/api";
import type { ScenarioTemplate, PaginatedResponse } from "@/lib/types";

export default function ScenariosPage() {
  const [scenarios, setScenarios] = useState<ScenarioTemplate[]>([]);
  const [editing, setEditing] = useState<ScenarioTemplate | null>(null);
  const [form, setForm] = useState({
    name: "",
    system_prompt: "",
    difficulty: "medium",
    intent: "",
    is_default: false,
  });
  const [showForm, setShowForm] = useState(false);

  const load = () =>
    apiGet<PaginatedResponse<ScenarioTemplate>>("/api/training/scenarios/").then((data) =>
      setScenarios(data.results)
    );

  useEffect(() => {
    load();
  }, []);

  const resetForm = () => {
    setForm({ name: "", system_prompt: "", difficulty: "medium", intent: "", is_default: false });
    setEditing(null);
    setShowForm(false);
  };

  const handleEdit = (s: ScenarioTemplate) => {
    setEditing(s);
    setForm({
      name: s.name,
      system_prompt: s.system_prompt,
      difficulty: s.difficulty,
      intent: s.intent,
      is_default: s.is_default,
    });
    setShowForm(true);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (editing) {
      await apiPut(`/api/training/scenarios/${editing.id}/`, form);
    } else {
      await apiPost("/api/training/scenarios/", form);
    }
    resetForm();
    load();
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this scenario?")) return;
    await apiDelete(`/api/training/scenarios/${id}/`);
    load();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Scenario Templates</h1>
          <p className="mt-1 text-gray-500">Define AI customer personas for training</p>
        </div>
        <button
          onClick={() => { resetForm(); setShowForm(true); }}
          className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700"
        >
          Add Scenario
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 space-y-4">
          <h3 className="text-lg font-semibold">
            {editing ? "Edit Scenario" : "New Scenario"}
          </h3>
          <div>
            <label className="block text-sm font-medium text-gray-700">Name</label>
            <input
              type="text"
              required
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700">
              System Prompt (AI customer instructions)
            </label>
            <textarea
              required
              value={form.system_prompt}
              onChange={(e) => setForm((f) => ({ ...f, system_prompt: e.target.value }))}
              rows={5}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700">Difficulty</label>
              <select
                value={form.difficulty}
                onChange={(e) => setForm((f) => ({ ...f, difficulty: e.target.value }))}
                className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
              >
                <option value="easy">Easy</option>
                <option value="medium">Medium</option>
                <option value="hard">Hard</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700">Intent</label>
              <input
                type="text"
                value={form.intent}
                onChange={(e) => setForm((f) => ({ ...f, intent: e.target.value }))}
                placeholder="e.g., Schedule service call"
                className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
              />
            </div>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="checkbox"
              id="is_default"
              checked={form.is_default}
              onChange={(e) => setForm((f) => ({ ...f, is_default: e.target.checked }))}
              className="rounded border-gray-300"
            />
            <label htmlFor="is_default" className="text-sm text-gray-700">
              Set as default scenario
            </label>
          </div>
          <div className="flex gap-3">
            <button
              type="submit"
              className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700"
            >
              {editing ? "Update" : "Create"}
            </button>
            <button type="button" onClick={resetForm} className="px-4 py-2 border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50">
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className="space-y-4">
        {scenarios.map((s) => (
          <div key={s.id} className="bg-white rounded-lg shadow p-6">
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="text-lg font-semibold text-gray-900">{s.name}</h3>
                  <span className={`px-2 py-0.5 text-xs rounded-full ${
                    s.difficulty === "easy" ? "bg-green-100 text-green-800" :
                    s.difficulty === "medium" ? "bg-yellow-100 text-yellow-800" :
                    "bg-red-100 text-red-800"
                  }`}>
                    {s.difficulty}
                  </span>
                  {s.is_default && (
                    <span className="px-2 py-0.5 text-xs rounded-full bg-indigo-100 text-indigo-800">
                      default
                    </span>
                  )}
                </div>
                {s.intent && <p className="mt-1 text-sm text-gray-500">Intent: {s.intent}</p>}
                <p className="mt-2 text-sm text-gray-600 line-clamp-2">{s.system_prompt}</p>
              </div>
              <div className="flex gap-2">
                <button onClick={() => handleEdit(s)} className="text-sm text-indigo-600 hover:text-indigo-500">Edit</button>
                <button onClick={() => handleDelete(s.id)} className="text-sm text-red-600 hover:text-red-500">Delete</button>
              </div>
            </div>
          </div>
        ))}
        {scenarios.length === 0 && (
          <p className="text-center text-gray-400 py-8">
            No scenarios yet. Create one to start training.
          </p>
        )}
      </div>
    </div>
  );
}
