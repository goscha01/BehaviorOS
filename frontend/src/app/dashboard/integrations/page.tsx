"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  catalogEntry,
  INTEGRATION_CATALOG,
  listIntegrations,
  runSyncIntegration,
  testIntegration,
  upsertIntegration,
  type IntegrationCatalogEntry,
  type IntegrationProvider,
  type SourceIntegration,
  type SyncStatus,
} from "@/lib/learning";

export default function IntegrationsPage() {
  const [backendItems, setBackendItems] = useState<
    Record<string, SourceIntegration>
  >({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<{
    catalog: IntegrationCatalogEntry;
    row: SourceIntegration | null;
  } | null>(null);

  const refresh = () => {
    setLoading(true);
    setError("");
    listIntegrations()
      .then((rows) => {
        const map: Record<string, SourceIntegration> = {};
        for (const r of rows) map[r.source_system] = r;
        setBackendItems(map);
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : "Failed to load.")
      )
      .finally(() => setLoading(false));
  };

  useEffect(refresh, []);

  const activeSources = INTEGRATION_CATALOG.filter(
    (e) => e.status === "available"
  );
  const viaCallio = INTEGRATION_CATALOG.filter(
    (e) => e.status === "coming_soon" && e.provider === "callio"
  );
  const direct = INTEGRATION_CATALOG.filter(
    (e) => e.status === "coming_soon" && e.provider === "direct"
  );

  return (
    <div className="space-y-8">
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
          unconfigured its adapter falls back to bundled fixtures.
        </p>
      </div>

      {loading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : error ? (
        <p className="text-sm text-rose-600">{error}</p>
      ) : (
        <>
          <Section
            title="Active"
            subtitle="These sources have live adapters. Connect a URL + token to start pulling real data; otherwise BehaviorOS uses bundled fixtures."
          >
            <div className="grid gap-4 md:grid-cols-2">
              {activeSources.map((entry) => (
                <ActiveIntegrationCard
                  key={entry.source_system}
                  entry={entry}
                  row={backendItems[entry.source_system] ?? null}
                  onEdit={(row) => setEditing({ catalog: entry, row })}
                  onSynced={refresh}
                />
              ))}
            </div>
          </Section>

          <Section
            title="Coming soon — via Callio / Sigcore"
            subtitle="These connect through Callio's shared communications platform. One connector at the platform layer, no duplicated OAuth or webhooks in BehaviorOS."
            providerBadge="callio"
          >
            <div className="grid gap-3 md:grid-cols-3">
              {viaCallio.map((entry) => (
                <ComingSoonCard key={entry.source_system} entry={entry} />
              ))}
            </div>
          </Section>

          <Section
            title="Coming soon — direct to BehaviorOS"
            subtitle="Systems Callio doesn't own. BehaviorOS will run these adapters natively — CRMs, ad platforms, payment/accounting systems, and third-party dispatch tools."
            providerBadge="direct"
          >
            <div className="grid gap-3 md:grid-cols-3">
              {direct.map((entry) => (
                <ComingSoonCard key={entry.source_system} entry={entry} />
              ))}
            </div>
          </Section>
        </>
      )}

      {editing && (
        <IntegrationDialog
          entry={editing.catalog}
          row={editing.row}
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

function Section({
  title,
  subtitle,
  providerBadge,
  children,
}: {
  title: string;
  subtitle: string;
  providerBadge?: IntegrationProvider;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-lg font-semibold text-gray-900">{title}</h2>
        {providerBadge === "callio" && (
          <span className="rounded-full bg-purple-100 px-2 py-0.5 text-xs font-medium text-purple-800">
            via Callio
          </span>
        )}
        {providerBadge === "direct" && (
          <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-800">
            direct
          </span>
        )}
      </div>
      <p className="mb-4 text-sm text-gray-600">{subtitle}</p>
      {children}
    </section>
  );
}

function statusPill(
  status: SyncStatus,
  hasUrl: boolean
): { label: string; className: string } {
  if (!hasUrl)
    return { label: "Not connected", className: "bg-gray-100 text-gray-700" };
  if (status === "ok")
    return { label: "Connected", className: "bg-emerald-100 text-emerald-800" };
  if (status === "error")
    return { label: "Error", className: "bg-rose-100 text-rose-800" };
  return { label: "Configured", className: "bg-blue-100 text-blue-800" };
}

function ActiveIntegrationCard({
  entry,
  row,
  onEdit,
  onSynced,
}: {
  entry: IntegrationCatalogEntry;
  row: SourceIntegration | null;
  onEdit: (row: SourceIntegration | null) => void;
  onSynced: () => void;
}) {
  const hasUrl = !!row?.url;
  const status: SyncStatus = row?.last_sync_status ?? "never";
  const pill = statusPill(status, hasUrl);
  const [testing, setTesting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [banner, setBanner] = useState<{
    tone: "ok" | "err";
    text: string;
  } | null>(null);

  async function handleTest() {
    setTesting(true);
    setBanner(null);
    try {
      const r = await testIntegration(entry.source_system);
      if (r.ok) {
        setBanner({
          tone: "ok",
          text: `Connection OK (HTTP ${r.http_status}${
            r.record_count != null ? `, ${r.record_count} records` : ""
          })`,
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
      const r = await runSyncIntegration(entry.source_system);
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
              {entry.label}
            </h3>
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-medium ${pill.className}`}
            >
              {pill.label}
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-600">{entry.description}</p>
        </div>
      </div>

      <dl className="mt-3 space-y-1 text-sm">
        <div className="flex justify-between">
          <dt className="text-gray-500">URL</dt>
          <dd className="max-w-[60%] truncate font-mono text-xs text-gray-700">
            {row?.url || (
              <span className="italic text-gray-400">
                not set — using fixture
              </span>
            )}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-gray-500">Token</dt>
          <dd className="font-mono text-xs text-gray-700">
            {row?.token_preview || (
              <span className="italic text-gray-400">not set</span>
            )}
          </dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-gray-500">Last sync</dt>
          <dd className="text-xs text-gray-700">
            {row?.last_synced_at
              ? `${new Date(row.last_synced_at).toLocaleString()} — ${
                  row.last_sync_created
                } new / ${row.last_sync_updated} updated`
              : "never"}
          </dd>
        </div>
      </dl>

      {row?.last_sync_error && (
        <p className="mt-2 rounded bg-rose-50 p-2 text-xs text-rose-800">
          {row.last_sync_error}
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
          onClick={() => onEdit(row)}
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

function ComingSoonCard({ entry }: { entry: IntegrationCatalogEntry }) {
  return (
    <div className="rounded-lg border border-dashed border-gray-300 bg-gray-50 p-4">
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-800">{entry.label}</h3>
        <span className="shrink-0 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
          Coming soon
        </span>
      </div>
      <p className="mt-1 text-xs text-gray-600 line-clamp-3">
        {entry.description}
      </p>
      <button
        type="button"
        disabled
        className="mt-3 w-full cursor-not-allowed rounded-md border border-gray-200 bg-white px-3 py-1.5 text-xs font-medium text-gray-400"
      >
        Connect
      </button>
    </div>
  );
}

function IntegrationDialog({
  entry,
  row,
  onClose,
  onSaved,
}: {
  entry: IntegrationCatalogEntry;
  row: SourceIntegration | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [url, setUrl] = useState(row?.url ?? "");
  const [token, setToken] = useState("");
  const [isActive, setIsActive] = useState(row?.is_active ?? true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      await upsertIntegration({
        source_system: entry.source_system,
        url: url.trim(),
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
          Configure {entry.label}
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
          placeholder={`https://${entry.source_system}.example.com/api/v1/evidence`}
          className="mt-1 w-full rounded-md border border-gray-300 p-2 text-sm focus:border-indigo-500 focus:outline-none"
          required
        />

        <label className="mt-4 block text-sm font-medium text-gray-700">
          Service token{" "}
          {row?.token_preview && (
            <span className="ml-2 text-xs text-gray-500">
              current: {row.token_preview} — leave blank to keep
            </span>
          )}
        </label>
        <input
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={
            row?.token_preview
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
