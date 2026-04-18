"use client";

import { useEffect, useState } from "react";
import { apiGet, apiPost, apiPut, apiDelete } from "@/lib/api";
import type { BusinessProfile, PaginatedResponse } from "@/lib/types";

export default function BusinessProfilePage() {
  const [profiles, setProfiles] = useState<BusinessProfile[]>([]);
  const [editing, setEditing] = useState<BusinessProfile | null>(null);
  const [form, setForm] = useState({
    name: "",
    service_desc: "",
    coverage_area: "",
    hours: "",
    pricing_notes: "",
  });
  const [showForm, setShowForm] = useState(false);

  const load = () =>
    apiGet<PaginatedResponse<BusinessProfile>>("/api/training/business-profiles/").then(
      (data) => setProfiles(data.results)
    );

  useEffect(() => {
    load();
  }, []);

  const resetForm = () => {
    setForm({ name: "", service_desc: "", coverage_area: "", hours: "", pricing_notes: "" });
    setEditing(null);
    setShowForm(false);
  };

  const handleEdit = (p: BusinessProfile) => {
    setEditing(p);
    setForm({
      name: p.name,
      service_desc: p.service_desc,
      coverage_area: p.coverage_area,
      hours: p.hours,
      pricing_notes: p.pricing_notes,
    });
    setShowForm(true);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (editing) {
      await apiPut(`/api/training/business-profiles/${editing.id}/`, form);
    } else {
      await apiPost("/api/training/business-profiles/", form);
    }
    resetForm();
    load();
  };

  const handleDelete = async (id: string) => {
    if (!confirm("Delete this business profile?")) return;
    await apiDelete(`/api/training/business-profiles/${id}/`);
    load();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Business Profiles</h1>
          <p className="mt-1 text-gray-500">
            Define your company details for training scenarios
          </p>
        </div>
        <button
          onClick={() => { resetForm(); setShowForm(true); }}
          className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700"
        >
          Add Profile
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow p-6 space-y-4">
          <h3 className="text-lg font-semibold">
            {editing ? "Edit Profile" : "New Profile"}
          </h3>
          {(
            [
              ["name", "Company Name", "text"],
              ["service_desc", "Service Description", "textarea"],
              ["coverage_area", "Coverage Area", "text"],
              ["hours", "Business Hours", "text"],
              ["pricing_notes", "Pricing Notes", "textarea"],
            ] as const
          ).map(([field, label, type]) => (
            <div key={field}>
              <label className="block text-sm font-medium text-gray-700">{label}</label>
              {type === "textarea" ? (
                <textarea
                  value={form[field]}
                  onChange={(e) => setForm((f) => ({ ...f, [field]: e.target.value }))}
                  rows={3}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
                />
              ) : (
                <input
                  type="text"
                  value={form[field]}
                  onChange={(e) => setForm((f) => ({ ...f, [field]: e.target.value }))}
                  className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
                />
              )}
            </div>
          ))}
          <div className="flex gap-3">
            <button
              type="submit"
              className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-700"
            >
              {editing ? "Update" : "Create"}
            </button>
            <button
              type="button"
              onClick={resetForm}
              className="px-4 py-2 border border-gray-300 rounded-md text-sm font-medium text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      <div className="space-y-4">
        {profiles.map((p) => (
          <div key={p.id} className="bg-white rounded-lg shadow p-6">
            <div className="flex items-start justify-between">
              <div>
                <h3 className="text-lg font-semibold text-gray-900">{p.name}</h3>
                <p className="mt-1 text-sm text-gray-600">{p.service_desc}</p>
                {p.coverage_area && (
                  <p className="mt-1 text-sm text-gray-500">Area: {p.coverage_area}</p>
                )}
                {p.hours && (
                  <p className="text-sm text-gray-500">Hours: {p.hours}</p>
                )}
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => handleEdit(p)}
                  className="text-sm text-indigo-600 hover:text-indigo-500"
                >
                  Edit
                </button>
                <button
                  onClick={() => handleDelete(p.id)}
                  className="text-sm text-red-600 hover:text-red-500"
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        ))}
        {profiles.length === 0 && (
          <p className="text-center text-gray-400 py-8">
            No business profiles yet. Create one to get started.
          </p>
        )}
      </div>
    </div>
  );
}
