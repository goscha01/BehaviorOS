# BehaviorOS
intelligence &amp; control plane
Here’s a **ready-to-paste task** for your Code AI agent to build **BehaviorOS v0** with the core infrastructure + the first module: **Dispatcher Training (ElevenLabs)**.

---

## Task: Build BehaviorOS v0 (Auth + Billing + Dispatcher Training)

### Goal

Create a production-ready **MVP web app** called **BehaviorOS** with:

1. **Registration + Login**
2. **Billing (Stripe subscriptions)**
3. **Core infrastructure** (multi-tenant-ready, audit logs, RBAC-lite, email, deployments)
4. **Dispatcher Training module** that runs voice roleplays using **ElevenLabs** (optionally OpenAI for conversation logic) and saves results.

---

## Recommended stack (keep it simple, non-React)

* **Backend + Web:** Django + Django REST Framework (or Django-only with server-rendered pages)
* **Auth:** Django Allauth (email/password) + optional magic link
* **DB:** Postgres
* **Async jobs:** Celery + Redis (for session orchestration, webhooks, evaluation jobs)
* **Billing:** Stripe (Checkout + Customer Portal + Webhooks)
* **File storage:** S3-compatible (or local for dev)
* **Deployment:** Docker Compose for dev, production deploy target (Render/Fly.io/DigitalOcean) with env vars

*(If you prefer Node instead, translate this spec to NestJS + Postgres + Stripe. But keep the same objects + flows.)*

---

## Core Product Flows

### 1) Authentication & Organization model (multi-tenant-ready)

* User can register/login.
* On first signup, create an **Organization** and set the user as **Owner**.
* Future: invite members (not required for v0, but schema should support it).

**Roles v0:** Owner, Admin, Member (simple RBAC).

---

### 2) Billing (Stripe)

Implement Stripe subscription billing with:

* Plans: `starter`, `pro` (monthly)
* Metering: optional later; for now, enforce plan limits in code.

**Required features**

* Stripe Checkout for starting subscription
* Stripe Customer Portal for manage/cancel/update card
* Webhooks to keep local subscription state in sync:

  * checkout.session.completed
  * customer.subscription.created/updated/deleted
  * invoice.paid / invoice.payment_failed

**App behavior**

* If org has no active subscription → training feature locked.
* Admin pages show subscription status, plan, renewal date.

---

### 3) Dispatcher Training module (ElevenLabs)

A dispatcher candidate is trained via **script + business context + scenario templates**.

**User story**
As an org admin, I can:

* Create **Business Profile** (company name, services, coverage area, pricing notes, policies)
* Create **Training Script** (uploaded text or pasted)
* Start a **Training Session** where AI plays a “customer” and speaks via ElevenLabs
* Candidate responds (voice or text for v0; voice preferred if feasible)
* Session is recorded and saved (audio + transcript)
* System produces structured outputs (not “evaluation app” positioning—just signals + notes):

  * extracted fields (did they confirm address? price? schedule?)
  * flags (overpromised, missed policy, unclear)
  * outcome (pass/review/fail) based on rubric template

**MVP scope option (choose one, but implement cleanly)**

* **Option A (fast):** Candidate responds by **text**; AI responds with ElevenLabs voice. (No mic handling complexity)
* **Option B (better):** Candidate responds via **browser mic**, send audio to STT (OpenAI Whisper or other), then continue.

I recommend Option A first, but code should allow Option B later.

---

## Data Model (minimum tables)

### Organizations / Users

* Organization(id, name, created_at)
* Membership(id, org_id, user_id, role)

### Billing

* StripeCustomer(org_id, stripe_customer_id)
* Subscription(org_id, stripe_subscription_id, status, plan, current_period_end, cancel_at_period_end)

### Training

* BusinessProfile(org_id, name, service_desc, policies_json, pricing_notes, hours, etc.)
* ScenarioTemplate(org_id, name, system_prompt, difficulty, intent, rubric_json, is_default)
* Script(org_id, name, content, version, created_at)

### Sessions

* TrainingSession(
  id, org_id, business_profile_id, scenario_template_id, script_id,
  status [created/running/completed/failed],
  started_at, ended_at
  )
* SessionTurn(
  id, session_id, speaker [ai/candidate], text, audio_url, created_at, metadata_json
  )
* SessionResult(
  session_id, outcome [pass/review/fail],
  signals_json, notes, created_at
  )

### Audit

* AuditLog(org_id, user_id, action, object_type, object_id, metadata_json, created_at)

---

## API / Pages

### Pages (server-rendered is fine)

* /register, /login, /logout
* /dashboard (subscription status + quick links)
* /billing (plan select + manage)
* /training (list sessions)
* /training/new (select business profile + scenario + script)
* /training/session/:id (run session UI)
* /training/session/:id/result (signals + transcript + audio playback)
* /settings/business-profile
* /settings/scenarios
* /settings/scripts

### API endpoints (even if using server-rendered UI, expose these)

* POST /api/training/sessions
* POST /api/training/sessions/:id/start
* POST /api/training/sessions/:id/turn (candidate input)
* GET  /api/training/sessions/:id
* GET  /api/training/sessions/:id/result

---

## ElevenLabs Integration Requirements

* Store ElevenLabs API key per environment (not per tenant for v0 unless needed)
* Create a small service wrapper:

  * `generate_speech(text, voice_id, model, stability, similarity_boost) -> audio_file_url`
* Persist AI audio outputs in SessionTurn.audio_url
* Make the voice configurable per org later; for now pick one voice_id in env.

---

## Conversation Orchestration (core logic)

Implement a “session runner” that:

* Initializes AI persona from ScenarioTemplate + BusinessProfile + Script
* Maintains conversation state (turns list)
* On each candidate message:

  * produce AI next response (OpenAI or deterministic prompt logic)
  * generate voice via ElevenLabs
  * save turn
* On completion:

  * generate `signals_json` and `outcome` from rubric_json + transcript

**Important**: Keep “signals extraction” separate from “chat generation”.

* `generate_reply()`
* `extract_signals_and_outcome()`

---

## Security / Compliance basics

* Password hashing стандарт (Django)
* CSRF protection
* Rate limiting on auth endpoints (basic)
* Webhook signature verification for Stripe
* Store secrets in env vars only
* Multi-tenant enforcement: every query scoped to org

---

## DevOps / Infra deliverables

* Docker Compose: app + postgres + redis
* `.env.example`
* Basic CI (lint + tests)
* Migration scripts
* Seed data command to create demo org + scenario + script
* Minimal tests:

  * auth flow
  * billing webhook sync
  * training session create/start/turn/complete

---

## Definition of Done

* User can sign up, log in, create org automatically
* User can subscribe via Stripe Checkout; subscription gates training
* Admin can create Business Profile + Script + Scenario Template
* Admin can run a training session:

  * AI speaks via ElevenLabs (audio playback works)
  * Candidate responds (text at minimum)
  * Transcript stored
  * Result page shows structured `signals_json`, outcome, and full transcript
* Stripe webhooks reliably update subscription status
* App runs locally with Docker Compose

---

## Nice-to-have (only if time remains)

* Invite users by email
* Candidate “guest mode” link (no account) for interviews
* Export session result to PDF
* Add session difficulty levels

---

If you want, I can also produce:

* the **exact prompt** to paste into your Code AI tool (Cursor/Claude Code) including file structure expectations, or
* a **Stripe plan structure + limits** (Starter/Pro with included sessions/minutes) that matches your broader BehaviorOS roadmap.
