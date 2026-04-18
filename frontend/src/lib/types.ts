export interface User {
  id: number;
  username: string;
  email: string;
  date_joined: string;
}

export interface Organization {
  id: string;
  name: string;
  created_at: string;
  role: "owner" | "admin" | "member" | null;
}

export interface AuthTokens {
  access: string;
  refresh: string;
}

export interface Subscription {
  id: string | null;
  plan: "starter" | "pro" | null;
  status: "active" | "past_due" | "canceled" | "incomplete" | "trialing" | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  created_at: string | null;
}

export interface BusinessProfile {
  id: string;
  name: string;
  service_desc: string;
  policies: Record<string, string>;
  pricing_notes: string;
  hours: string;
  coverage_area: string;
  created_at: string;
  updated_at: string;
}

export interface ScenarioTemplate {
  id: string;
  name: string;
  system_prompt: string;
  difficulty: "easy" | "medium" | "hard";
  intent: string;
  rubric: Record<string, unknown>;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface Script {
  id: string;
  name: string;
  content: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface SessionTurn {
  id: string;
  speaker: "ai" | "candidate";
  text: string;
  audio_url: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface SessionResult {
  id: string;
  outcome: "pass" | "review" | "fail";
  signals: {
    signals?: Record<string, boolean>;
    flags?: string[];
  };
  notes: string;
  created_at: string;
}

export interface TrainingSession {
  id: string;
  business_profile: string;
  scenario_template: string;
  script: string;
  business_profile_name: string | null;
  scenario_name: string | null;
  script_name: string | null;
  status: "created" | "running" | "completed" | "failed";
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  turns: SessionTurn[];
  result: SessionResult | null;
}

export interface TrainingSessionListItem {
  id: string;
  business_profile_name: string | null;
  scenario_name: string | null;
  status: "created" | "running" | "completed" | "failed";
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  has_result: boolean;
}

export interface PaginatedResponse<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}
