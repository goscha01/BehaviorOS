/**
 * Learning engine types + API helpers.
 *
 * Kept in one file so contributors touching the review queue have a
 * single source of truth for the API shape. Keep this file in lockstep
 * with `apps/learning/serializers.py`.
 */

import { apiGet, apiPost } from "./api";

// ---------- Types ----------

export type SuggestionStatus =
  | "new"
  | "under_review"
  | "approved"
  | "implemented"
  | "measured"
  | "archived"
  | "rejected"
  | "watchlist";

export type SuggestionCategory =
  | "pricing"
  | "faq"
  | "qualification"
  | "playbook"
  | "missing_info"
  | "tone"
  | "other";

export type OutcomeSignal = "positive" | "negative" | "neutral";

export interface ConfidenceBreakdown {
  llm: number;
  llm_synthesis: number;
  support: number;
  support_count: number;
  outcome_consistency: number;
  final: number;
}

export interface SuggestionListItem {
  id: string;
  title: string;
  category: SuggestionCategory;
  status: SuggestionStatus;
  confidence: string; // Decimal serialized as string
  supporting_count: number;
  created_at: string;
  updated_at: string;
}

export interface RepresentativeExample {
  candidate_id: string;
  evidence_id: string;
  source_system: string;
  evidence_type: string;
  external_id: string;
  title: string;
  llm_confidence: number;
  outcome_signal: OutcomeSignal;
}

export interface RepresentativeExamples {
  top: RepresentativeExample[];
  highest_confidence: RepresentativeExample | null;
  newest: RepresentativeExample | null;
}

export interface SuggestionDetail {
  id: string;
  title: string;
  description: string;
  category: SuggestionCategory;
  status: SuggestionStatus;
  confidence: string;
  confidence_breakdown: ConfidenceBreakdown;
  supporting_count: number;
  representative_examples: RepresentativeExamples;
  synthesis_json: {
    why_this_matters?: string;
    supporting_evidence_summary?: string;
    suggested_playbook_change?: string;
    suggested_faq_addition?: string;
    publish_receipts?: Record<string, unknown>;
    raw_response?: string;
  };
  synthesis_model: string;
  synthesis_prompt_version: string;
  publish_targets: string[];
  impact_json: Record<string, unknown>;
  review_note: string;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
  // Derived answers to the four review-page questions:
  reason_generated: {
    summary: string;
    confidence_breakdown: ConfidenceBreakdown;
    synthesis_model: string;
    synthesis_prompt_version: string;
  };
  supporting_conversations: {
    candidate_count: number;
    distinct_evidence_count: number;
    representative: RepresentativeExamples;
  };
  outcome_distribution: {
    counts: { positive?: number; negative?: number; neutral?: number };
    consistency: number;
  };
  proposed_changes: {
    playbook_change: string;
    faq_addition: string;
    why_this_matters: string;
    publish_targets: string[];
  };
}

export interface SupportingEvidenceItem {
  id: string;
  kind: "playbook_rule" | "faq";
  category: SuggestionCategory;
  title: string;
  description: string;
  llm_confidence: string;
  outcome_signal: OutcomeSignal;
  source_system: string;
  evidence_type: string;
  external_id: string;
  occurred_at: string | null;
  business_rules_version: string;
  evidence_summary: string;
  created_at: string;
}

export interface PaginatedResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface TransitionResponse {
  suggestion: SuggestionDetail;
  side_effects: Record<string, unknown>;
}

// ---------- API helpers ----------

const BASE = "/api/learning/suggestions";

export function listSuggestions(
  params: { status?: SuggestionStatus[]; category?: SuggestionCategory; page?: number } = {}
): Promise<PaginatedResponse<SuggestionListItem>> {
  const query = new URLSearchParams();
  if (params.status?.length) query.set("status", params.status.join(","));
  if (params.category) query.set("category", params.category);
  if (params.page) query.set("page", String(params.page));
  const qs = query.toString();
  return apiGet<PaginatedResponse<SuggestionListItem>>(`${BASE}/${qs ? `?${qs}` : ""}`);
}

export function getSuggestion(id: string): Promise<SuggestionDetail> {
  return apiGet<SuggestionDetail>(`${BASE}/${id}/`);
}

export function getSupportingEvidence(
  id: string,
  page = 1
): Promise<PaginatedResponse<SupportingEvidenceItem>> {
  return apiGet<PaginatedResponse<SupportingEvidenceItem>>(
    `${BASE}/${id}/supporting-evidence/?page=${page}`
  );
}

export function startReview(id: string): Promise<TransitionResponse> {
  return apiPost<TransitionResponse>(`${BASE}/${id}/start-review/`);
}

export function approveSuggestion(
  id: string,
  body: { note?: string; publish_to?: string[] } = {}
): Promise<TransitionResponse> {
  return apiPost<TransitionResponse>(`${BASE}/${id}/approve/`, body);
}

export function rejectSuggestion(
  id: string,
  reason: string
): Promise<TransitionResponse> {
  return apiPost<TransitionResponse>(`${BASE}/${id}/reject/`, { reason });
}

export function markImplemented(
  id: string,
  publish_receipts: Record<string, unknown> = {}
): Promise<TransitionResponse> {
  return apiPost<TransitionResponse>(`${BASE}/${id}/mark-implemented/`, {
    publish_receipts,
  });
}

export function markMeasured(
  id: string,
  impact: Record<string, unknown>
): Promise<TransitionResponse> {
  return apiPost<TransitionResponse>(`${BASE}/${id}/mark-measured/`, { impact });
}

export function archiveSuggestion(id: string): Promise<TransitionResponse> {
  return apiPost<TransitionResponse>(`${BASE}/${id}/archive/`);
}

// ---- Morning brief + trends ----

export interface BriefSuggestion {
  id: string;
  title: string;
  category: SuggestionCategory;
  status: SuggestionStatus;
  confidence: string;
  supporting_count: number;
  created_at: string;
}

export interface BriefLastJob {
  id: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  evidence_processed: number;
  suggestions_created: number;
  cost_usd: string;
}

export interface MorningBrief {
  generated_at: string;
  window_days: number;
  since: string;
  last_job: BriefLastJob | null;
  new_suggestions_today: {
    count: number;
    since: string;
    top: BriefSuggestion[];
  };
  category_counts: Partial<Record<SuggestionCategory, number>>;
  trends: {
    top_supported: BriefSuggestion[];
    recurring_issues: BriefSuggestion[];
    recurring_opportunities: BriefSuggestion[];
  };
}

export interface TrendsResponse {
  window_days: number;
  since: string;
  top_supported: BriefSuggestion[];
  recurring_issues: BriefSuggestion[];
  recurring_opportunities: BriefSuggestion[];
}

export function getMorningBrief(windowDays = 30): Promise<MorningBrief> {
  return apiGet<MorningBrief>(`${BASE}/morning-brief/?window_days=${windowDays}`);
}

export function getTrends(windowDays = 30): Promise<TrendsResponse> {
  return apiGet<TrendsResponse>(`${BASE}/trends/?window_days=${windowDays}`);
}

// ---- Integrations ----

export type SyncStatus = "never" | "ok" | "error";

export interface SourceIntegration {
  id: string | null;
  source_system: string;
  url: string;
  token_preview: string;
  is_active: boolean;
  last_synced_at: string | null;
  last_sync_status: SyncStatus;
  last_sync_error: string;
  last_sync_created: number;
  last_sync_updated: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface TestConnectionResponse {
  ok: boolean;
  http_status?: number;
  record_count?: number | null;
  detail?: string;
}

export interface RunSyncResponse {
  ok: boolean;
  created: number;
  updated: number;
  error: string;
}

const INTEGRATIONS_BASE = "/api/learning/integrations";

export function listIntegrations(): Promise<SourceIntegration[]> {
  return apiGet<SourceIntegration[]>(`${INTEGRATIONS_BASE}/`);
}

export function upsertIntegration(body: {
  source_system: string;
  url: string;
  token?: string;
  is_active?: boolean;
}): Promise<SourceIntegration> {
  return apiPost<SourceIntegration>(`${INTEGRATIONS_BASE}/`, body);
}

export function testIntegration(sourceSystem: string): Promise<TestConnectionResponse> {
  return apiPost<TestConnectionResponse>(`${INTEGRATIONS_BASE}/${sourceSystem}/test/`);
}

export function runSyncIntegration(sourceSystem: string): Promise<RunSyncResponse> {
  return apiPost<RunSyncResponse>(`${INTEGRATIONS_BASE}/${sourceSystem}/run-sync/`);
}

// ---- Integration catalog ----
//
// Roadmap of every source BehaviorOS will consume evidence from. The
// active entries have real adapters (matching the /api/learning/integrations/
// backend list). The coming-soon entries render as disabled cards so the
// full map is visible from day one — architecturally split by whether the
// data flows through the Callio/Sigcore communications platform or comes
// direct to BehaviorOS.

export type IntegrationStatus = "available" | "coming_soon";

// Provider path — matters for how the connection is wired later:
// - "callio" sources register once against Callio/Sigcore's shared
//   provider API; BehaviorOS just subscribes to the normalized stream.
// - "direct" sources talk to BehaviorOS's own adapter (no Callio in the path).
export type IntegrationProvider = "active" | "callio" | "direct";

export interface IntegrationCatalogEntry {
  source_system: string;
  label: string;
  description: string;
  status: IntegrationStatus;
  provider: IntegrationProvider;
}

export const INTEGRATION_CATALOG: IntegrationCatalogEntry[] = [
  // --- Active adapters (Phase 1 canonical three) ---
  {
    source_system: "leadbridge",
    label: "LeadBridge",
    description:
      "Chat conversations from Thumbtack and Yelp with AI Playbook version + outcome.",
    status: "available",
    provider: "active",
  },
  {
    source_system: "callio",
    label: "Callio",
    description:
      "Voice call transcripts with speaker labels and captured lead.",
    status: "available",
    provider: "active",
  },
  {
    source_system: "serviceflow",
    label: "ServiceFlow",
    description:
      "Booking lifecycle events — booked, cancelled, completed, recurring, revenue.",
    status: "available",
    provider: "active",
  },

  // --- Coming soon via Callio/Sigcore (shared communications backbone) ---
  {
    source_system: "quo",
    label: "Quo",
    description:
      "Multi-channel messaging. Connects through Callio — no duplicated OAuth or webhooks.",
    status: "coming_soon",
    provider: "callio",
  },
  {
    source_system: "whatsapp",
    label: "WhatsApp",
    description: "Customer chats via Callio's WhatsApp channel.",
    status: "coming_soon",
    provider: "callio",
  },
  {
    source_system: "telegram",
    label: "Telegram",
    description: "Customer chats via Callio's Telegram channel.",
    status: "coming_soon",
    provider: "callio",
  },
  {
    source_system: "sms",
    label: "SMS",
    description: "Two-way SMS threads via Callio.",
    status: "coming_soon",
    provider: "callio",
  },
  {
    source_system: "messenger",
    label: "Messenger",
    description: "Facebook Messenger conversations via Callio.",
    status: "coming_soon",
    provider: "callio",
  },
  {
    source_system: "email",
    label: "Email",
    description: "Inbound + outbound email threads via Callio.",
    status: "coming_soon",
    provider: "callio",
  },

  // --- Coming soon direct-to-BehaviorOS (not owned by Callio) ---
  {
    source_system: "bookingkoala",
    label: "BookingKoala",
    description: "Booking lifecycle + reviews from BookingKoala.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "jobber",
    label: "Jobber",
    description: "Jobs, invoices, and client history from Jobber.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "housecallpro",
    label: "Housecall Pro",
    description: "Estimates, jobs, and customer notes from Housecall Pro.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "servicetitan",
    label: "ServiceTitan",
    description: "Enterprise jobs, quotes, and technicians from ServiceTitan.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "google_ads",
    label: "Google Ads",
    description: "Campaign spend + conversions from Google Ads.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "google_business_profile",
    label: "Google Business Profile",
    description: "Reviews, Q&A, and messages from GBP.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "facebook_ads",
    label: "Facebook Ads",
    description: "Campaign spend + lead-form submissions from Facebook Ads.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "stripe",
    label: "Stripe",
    description: "Payments, refunds, and churn signals from Stripe.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "quickbooks",
    label: "QuickBooks",
    description: "Invoices and payment history from QuickBooks.",
    status: "coming_soon",
    provider: "direct",
  },
  {
    source_system: "generic_crm",
    label: "Generic CRM",
    description: "Custom CRM webhook — for CRMs BehaviorOS doesn't have a first-class adapter for yet.",
    status: "coming_soon",
    provider: "direct",
  },
];

export function catalogEntry(sourceSystem: string): IntegrationCatalogEntry | undefined {
  return INTEGRATION_CATALOG.find((e) => e.source_system === sourceSystem);
}

export function sourceLabel(sourceSystem: string): string {
  return catalogEntry(sourceSystem)?.label ?? sourceSystem;
}

// Legacy alias for the old three-source metadata map (kept for any callers
// that haven't been updated to catalogEntry() yet).
export const SOURCE_META: Record<string, { label: string; description: string }> =
  Object.fromEntries(
    INTEGRATION_CATALOG.filter((e) => e.status === "available").map((e) => [
      e.source_system,
      { label: e.label, description: e.description },
    ])
  );


// ---------- UI helpers ----------

export const STATUS_LABEL: Record<SuggestionStatus, string> = {
  new: "New",
  under_review: "Under Review",
  approved: "Approved",
  implemented: "Implemented",
  measured: "Measured",
  archived: "Archived",
  rejected: "Rejected",
  watchlist: "Watchlist",
};

export const CATEGORY_LABEL: Record<SuggestionCategory, string> = {
  pricing: "Pricing",
  faq: "FAQ",
  qualification: "Qualification",
  playbook: "Playbook",
  missing_info: "Missing Info",
  tone: "Tone",
  other: "Other",
};

export function statusBadgeClass(status: SuggestionStatus): string {
  switch (status) {
    case "new":
      return "bg-blue-100 text-blue-800";
    case "under_review":
      return "bg-amber-100 text-amber-800";
    case "approved":
      return "bg-emerald-100 text-emerald-800";
    case "implemented":
      return "bg-indigo-100 text-indigo-800";
    case "measured":
      return "bg-purple-100 text-purple-800";
    case "archived":
      return "bg-gray-100 text-gray-700";
    case "rejected":
      return "bg-rose-100 text-rose-800";
    case "watchlist":
      return "bg-slate-100 text-slate-700";
  }
}
