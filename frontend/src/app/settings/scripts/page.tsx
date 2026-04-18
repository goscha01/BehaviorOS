"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost, apiPut, apiDelete } from "@/lib/api";
import type { Script, PaginatedResponse } from "@/lib/types";

export default function ScriptsPage() {
  const [scripts, setScripts] = useState<Script[]>([]);
  const [editing, setEditing] = useState<Script | null>(null);
  const [form, setForm] = useState({ name: "", content: "", version: 1 });
  const [showForm, setShowForm] = useState(false);

  const load = () =>
    apiGet<PaginatedResponse<Script>>("/api/training/scripts/").then((data) =>
      setScripts(data.results)
    );

  useEffect(() => {
    load();
  }, []);

  const resetForm = () => {
    setForm({ name: "", content: "", version: 1 });
    setEditing(null);
    setShowForm(false);
  };

  const handleEdit = (s: Script) => {
    setEditing(s);
    setForm({ name: s.name, content: s.content, version: s.version });
    setShowForm(true);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (editing) {
      await apiPut(`/api/training/scripts/${editing.id}/`, form);
    } else {
      await apiPost("/api/training/scripts/", form);
    }
    resetForm();
    load();
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this script?")) return;
    await apiDelete(`/api/training/scripts/${id}/`);
    load();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Training Scripts</h1>
          <p className="mt-1 text-gray-500">Define scripts for dispatchers to follow</p>
        </div>
        <button
          onClick={() => { resetForm(); setShowForm(true); }}
          className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700"
        >
          Add Script
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 space-y-4">
          <h3 className="text-lg font-semibold">
            {editing ? "Edit Script" : "New Script"}
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
              Script Content
            </label>
            <textarea
              required
              value={form.content}
              onChange={(e) => setForm((f) => ({ ...f, content: e.target.value }))}
              rows={10}
              placeholder="Greeting: 'Thank you for calling...'"
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 font-mono text-sm"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700">Version</label>
            <input
              type="number"
              min={1}
              value={form.version}
              onChange={(e) => setForm((f) => ({ ...f, version: parseInt(e.target.value) || 1 }))}
              className="mt-1 block w-32 rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="flex gap-3">
            <button type="submit" className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700">
              {editing ? "Update" : "Create"}
            </button>
            <button type="button" onClick={resetForm} className="px-4 py-2 border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50">
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className="space-y-4">
        {scripts.map((s) => (
          <div key={s.id} className="bg-white rounded-lg shadow p-6">
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-lg font-semibold text-gray-900">{s.name}</h3>
                <p className="text-sm text-gray-500">Version {s.version}</p>
                <pre className="mt-2 text-sm text-gray-600 whitespace-pre-wrap line-clamp-4">
                  {s.content}
                </pre>
              </div>
              <div className="flex gap-2">
                <button onClick={() => handleEdit(s)} className="text-sm text-indigo-600 hover:text-indigo-500">Edit</button>
                <button onClick={() => handleDelete(s.id)} className="text-sm text-red-600 hover:text-red-500">Delete</button>
              </div>
            </div>
          </div>
        ))}
        {scripts.length === 0 && (
          <p className="text-center text-gray-400 py-8">
            No scripts yet. Create one to get started.
          </p>
        )}
      </div>
    </div>
  );
}
