"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  listIntegrations,
  runSyncIntegration,
  SOURCE_META,
  sourceLabel,
  testIntegration,
  upsertIntegration,
  type SourceIntegration,
  type SyncStatus,
} from "@/lib/learning";

export default function IntegrationsPage() {
  const [items, setItems] = useState<SourceIntegration[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<SourceIntegration | null>(null);

  const refresh = () => {
    setLoading(true);
    setError("");
    listIntegrations()
      .then(setItems)
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Failed to load.")
      )
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/dashboard/learning"
          className="text-sm text-gray-500 hover:text-indigo-600"
        >
          ← Morning brief
        </Link>
        <h1 className="mt-1 text-2xl font-bold text-gray-900">Integrations</h1>
        <p className="mt-1 text-gray-500">
          Connect the systems BehaviorOS learns from. When a source is
          unconfigured, its adapter falls back to bundled fixtures so the
          pipeline still runs end-to-end.
        </p>
      </div>

      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : error ? (
        <p className="text-sm text-rose-600">{error}</p>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {items.map((item) => (
            <IntegrationCard
              key={item.source_system}
              item={item}
              onEdit={() => setEditing(item)}
              onSynced={refresh}
            />
          ))}
        </div>
      )}

      {editing && (
        <IntegrationDialog
          integration={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function statusPill(status: SyncStatus, hasUrl: boolean): {
  label: string;
  className: string;
} {
  if (!hasUrl) return { label: "Not connected", className: "bg-gray-100 text-gray-700" };
  if (status === "ok") return { label: "Connected", className: "bg-emerald-100 text-emerald-800" };
  if (status === "error") return { label: "Error", className: "bg-rose-100 text-rose-800" };
  return { label: "Configured", className: "bg-blue-100 text-blue-800" };
}

function IntegrationCard({
  item,
  onEdit,
  onSynced,
}: {
  item: SourceIntegration;
  onEdit: () => void;
  onSynced: () => void;
}) {
  const meta = SOURCE_META[item.source_system];
  const hasUrl = !!item.url;
  const pill = statusPill(item.last_sync_status, hasUrl);
  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "err"; text: string } | null>(null);

  async function handleTest() {
    setTesting(true);
    setBanner(null);
    try {
      const r = await testIntegration(item.source_system);
      if (r.ok) {
        setBanner({
          tone: "ok",
          text: `Connection OK (HTTP ${r.http_status}${r.record_count != null ? `, ${r.record_count} records` : ""})`,
        });
      } else {
        setBanner({ tone: "err", text: r.detail ?? "Connection failed." });
      }
    } catch (err) {
      setBanner({
        tone: "err",
        text: err instanceof Error ? err.message : "Test failed.",
      });
    } finally {
      setTesting(false);
    }
  }

  async function handleSync() {
    setSyncing(true);
    setBanner(null);
    try {
      const r = await runSyncIntegration(item.source_system);
      if (r.ok) {
        setBanner({
          tone: "ok",
          text: `Sync OK — ${r.created} new / ${r.updated} updated`,
        });
        onSynced();
      } else {
        setBanner({ tone: "err", text: r.error || "Sync failed." });
      }
    } catch (err) {
      setBanner({
        tone: "err",
        text: err instanceof Error ? err.message : "Sync failed.",
      });
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 shadow-sm">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold text-gray-900">
              {sourceLabel(item.source_system)}
            </h3>
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-medium ${pill.className}`}
            >
              {pill.label}
            </span>
          </div>
          {meta?.description && (
            <p className="mt-1 text-sm text-gray-600">{meta.description}</p>
          )}
        </div>
      </div>

      <dl className="mt-3 space-y-1 text-sm">
        <div className="flex justify-between">
          <dt className="text-gray-500">URL</dt>
          <dd className="max-w-[60%] truncate font-mono text-xs text-gray-700">
            {item.url || <span className="italic text-gray-400">not set — using fixture</span>}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-gray-500">Token</dt>
          <dd className="font-mono text-xs text-gray-700">
            {item.token_preview || <span className="italic text-gray-400">not set</span>}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-gray-500">Last sync</dt>
          <dd className="text-xs text-gray-700">
            {item.last_synced_at
              ? `${new Date(item.last_synced_at).toLocaleString()} — ${item.last_sync_created} new / ${item.last_sync_updated} updated`
              : "never"}
          </dd>
        </div>
      </dl>

      {item.last_sync_error && (
        <p className="mt-2 rounded bg-rose-50 p-2 text-xs text-rose-800">
          {item.last_sync_error}
        </p>
      )}

      {banner && (
        <p
          className={`mt-2 rounded p-2 text-xs ${
            banner.tone === "ok"
              ? "bg-emerald-50 text-emerald-800"
              : "bg-rose-50 text-rose-800"
          }`}
        >
          {banner.text}
        </p>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onEdit}
          className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50"
        >
          {hasUrl ? "Edit" : "Connect"}
        </button>
        <button
          type="button"
          onClick={handleTest}
          disabled={!hasUrl || testing}
          className="rounded-md border border-indigo-300 px-3 py-1.5 text-sm font-medium text-indigo-700 hover:bg-indigo-50 disabled:opacity-50"
        >
          {testing ? "Testing…" : "Test connection"}
        </button>
        <button
          type="button"
          onClick={handleSync}
          disabled={syncing}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {syncing ? "Syncing…" : "Run sync now"}
        </button>
      </div>
    </div>
  );
}

function IntegrationDialog({
  integration,
  onClose,
  onSaved,
}: {
  integration: SourceIntegration;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [url, setUrl] = useState(integration.url ?? "");
  const [token, setToken] = useState("");
  const [isActive, setIsActive] = useState(integration.is_active);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      await upsertIntegration({
        source_system: integration.source_system,
        url: url.trim(),
        // Blank token = keep existing (backend contract).
        ...(token.trim() ? { token: token.trim() } : {}),
        is_active: isActive,
      });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-lg rounded-lg bg-white p-6 shadow-xl"
      >
        <h2 className="text-lg font-semibold text-gray-900">
          Configure {sourceLabel(integration.source_system)}
        </h2>
        <p className="mt-1 text-sm text-gray-600">
          BehaviorOS will GET the URL with{" "}
          <code className="text-xs">Authorization: Bearer &lt;token&gt;</code>{" "}
          and expect a JSON list of records.
        </p>

        <label className="mt-4 block text-sm font-medium text-gray-700">
          Evidence endpoint URL
        </label>
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://leadbridge.example.com/api/v1/conversations"
          className="mt-1 w-full rounded-md border border-gray-300 p-2 text-sm focus:border-indigo-500 focus:outline-none"
          required
        />

        <label className="mt-4 block text-sm font-medium text-gray-700">
          Service token{" "}
          {integration.token_preview && (
            <span className="ml-2 text-xs text-gray-500">
              current: {integration.token_preview} — leave blank to keep
            </span>
          )}
        </label>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={
            integration.token_preview
              ? "Leave blank to keep the existing token"
              : "Bearer token"
          }
          className="mt-1 w-full rounded-md border border-gray-300 p-2 text-sm focus:border-indigo-500 focus:outline-none"
        />

        <label className="mt-4 flex items-center gap-2 text-sm text-gray-700">
          <input
            type="checkbox"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
          />
          Active — nightly job will pull from this source
        </label>

        {error && <p className="mt-3 text-sm text-rose-600">{error}</p>}

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-gray-300 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
            disabled={saving}
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={saving || !url.trim()}
            className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}
