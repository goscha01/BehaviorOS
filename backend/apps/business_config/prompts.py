"""LLM prompts for BusinessConfigProposal synthesis (V2).

Versioned strings — bump the version suffix whenever the shape or
instructions change so downstream can re-analyze / audit.

V2 changes vs v1:
  - pricing_model added as a first-class structured field
  - pricing_examples[] extracted separately (marked observed_example — NOT
    tenant-wide rules)
  - Explicit rule: DO NOT synthesize an hourly rate from flat-project quotes
  - Commercial policy fields added (materials_included, payment_methods)
  - Service-scope extraction added
  - FAQ prompt tightened with REJECT list for availability/timing, review
    requests, single-project timelines, and anything already covered by
    pricing/commercial/services extraction
  - Every emitted candidate carries a factKind label so the applier can
    treat observed examples differently from defensible rules
"""

PROMPT_VERSION_PRICING = 'business_config.pricing_commercial:v2'
PROMPT_VERSION_FAQ = 'business_config.faq:v2'


# ------------------------------------------------------------------------
# Pricing + Commercial extraction — one LLM call, structured output.
# ------------------------------------------------------------------------

PRICING_SYSTEM = """\
You analyze a small corpus of home-services (handyman/cleaning/etc.)
conversations to extract what the pro's ACTUAL pricing and commercial
practice is, as evidenced by the transcripts.

You will be given:
  1. The template's baseline pricing (usually zero/unset — the canonical
     shape the tenant started from).
  2. The tenant's CURRENT configured pricing (which the tenant explicitly
     entered during onboarding; treat as authoritative unless clearly
     contradicted by multiple conversations).
  3. A set of historical conversation transcripts, each with conversation_id.

# CRITICAL RULES

RULE 1 — Do not fabricate. If evidence is thin, say `insufficient_evidence`
truthfully. NEVER pad the output to fill fields.

RULE 2 — Do not synthesize an hourly rate from flat-project quotes. If the
pro consistently quoted flat prices per item or per project ($100 for deck
box assembly, $150 for sink replacement, etc.), the hourly_rate observation
MUST be null. DO NOT compute an implied hourly rate by dividing a flat quote
by an estimated hour count. Instead, mark pricing_model appropriately and
put the flat quotes in pricing_examples.

RULE 3 — Distinguish two levels of fact strength:
  - `explicit_rule`     — pro EXPLICITLY stated a general rule ("I always
                          charge $X"; "My rate is $Y/hr"; "Materials are
                          never included in my estimates"). Highest strength.
  - `inferred_rule`     — same pattern across ≥3 conversations, no explicit
                          statement. Medium strength.
  - `observed_example`  — one-off instance (this job cost $X). Concrete but
                          NOT generalizable to a tenant-wide rule.

# OUTPUT FIELDS

Return a single JSON object of the form:
{
  "pricing_model":         { observation-object },
  "hourly_rate":           { observation-object },
  "minimum_hours":         { observation-object },
  "minimum_charge":        { observation-object },
  "quote_required":        { observation-object },
  "materials_included":    { observation-object },
  "payment_methods":       { observation-object; observed_value is array-of-strings },
  "services_observed":     { observation-object; observed_value is array-of-strings },
  "pricing_examples":      [ example-object, ... ]
}

## observation-object

  {
    "observed_value":      the value observed (type varies per field — see below);
                           null when insufficient evidence
    "fact_kind":           "explicit_rule" | "inferred_rule" | "observed_example" | null
    "confidence":          float in [0, 1]
    "supporting_conversation_ids": array of conversation_ids
    "representative_snippet": short verbatim quote from one supporting
                              conversation (include speaker prefix)
    "reasoning":           one sentence
  }

## Field expectations

  pricing_model         one of: "hourly" | "flat_project" | "itemized" |
                                "hybrid" | "unclear"
                        Set to whichever the pro DEMONSTRABLY used in these
                        conversations. If pro switched between models, use
                        "hybrid". If unclear/no pricing conversation,
                        "unclear" (null observed_value).

  hourly_rate           number (USD/hr) or null. NULL if no explicit hourly
                        quote appeared. See RULE 2.

  minimum_hours         number or null

  minimum_charge        number or null

  quote_required        true/false/null — did the pro insist on producing a
                        quote before booking?

  materials_included    true/false/null — do the pro's labor estimates
                        include materials, or are materials extra? Only fill
                        if pro explicitly clarified.

  payment_methods       observed_value is an ARRAY of strings the pro
                        actually accepted (e.g. ["Zelle", "cash"]). Empty
                        array if not observed.

  services_observed     observed_value is an ARRAY of specific service
                        strings the pro actually performed (e.g.
                        ["furniture assembly", "sink replacement",
                        "patio repair"]). Focus on the specific work
                        performed, not the general category.

## pricing_examples

  Concrete price observations for specific jobs. Each is an observed_example
  by construction. Use this when the pro quoted a flat/project price for a
  specific job — those are not tenant-wide rules but are useful reference.

  Each example-object:
    {
      "item":            short description of the job (e.g. "Keter deck box
                         assembly (2 units)")
      "price":           number (USD)
      "unit":            "flat" | "per_item" | "hourly" | "unknown"
      "supporting_conversation_id": string
      "representative_snippet": short verbatim quote
    }

  If none observed, return [].

# STYLE

No markdown, no prose outside the JSON. Do not include fields I did not
list. Keep snippets short (≤400 chars).
"""


# ------------------------------------------------------------------------
# FAQ extraction — genuine durable Q&A only. Heavily filtered.
# ------------------------------------------------------------------------

FAQ_SYSTEM = """\
You analyze a small corpus of home-services conversations to extract
DURABLE customer questions that a pro should be prepared to answer
consistently — the kind of information that belongs in an FAQ.

You will be given:
  1. The template's baseline FAQ (usually empty).
  2. The tenant's CURRENT FAQ (may be empty).
  3. Historical conversation transcripts with conversation_ids.
  4. NOTE: pricing, commercial policies (materials, payment), and specific
     service-scope details are extracted separately. Do not duplicate those
     here.

# CRITICAL — WHAT TO REJECT

Do NOT emit an FAQ candidate for any of these, no matter how often they
appear in the corpus:

  1. Availability / response speed / arrival time / same-day promises
     ("How soon can you come?", "Can you do it today?"). These reflect
     historical availability at the time of those conversations, not
     durable business policy. The runtime uses current calendar state.

  2. Requests for reviews / thank-you-for-the-review messages. These
     belong to a future post-service workflow, not the AI's Q&A layer.

  3. Single-project timelines ("How long will the patio take?" → "2-3
     days"). Project-specific durations don't generalize to a tenant-wide
     fact. Only include timeline questions if the answer is a GENERAL
     rule the pro would give for that category of work.

  4. Anything already covered by the pricing/commercial extraction:
     hourly rate, minimum hours, minimum charge, payment methods,
     materials included/excluded, specific price quotes. Those go into
     the pricing prompt, not here.

  5. Greetings, pleasantries, out-of-office messages, or one-off
     scheduling exchanges.

# WHAT TO EMIT

Only include a candidate if ALL of these hold:
  - The customer asked a question of the kind that WILL recur.
  - The pro's answer is a durable business fact (not a per-project
    detail).
  - The answer is either explicit ("Yes, I'm licensed and insured") OR
    consistently stated across ≥2 conversations.

# OUTPUT

Return a single JSON object of the form:
  { "candidates": [ candidate-object, ... ] }

candidate-object:
  {
    "field_key":              stable slug identifier (kebab/snake case,
                              lowercase; e.g. "insurance_status")
    "human_label":            short display-friendly label
    "question":               customer-facing wording
    "answer":                 the pro's answer (verbatim if possible)
    "fact_kind":              "explicit_rule" | "inferred_rule"
                              (never "observed_example" — that's not FAQ)
    "confidence":             float in [0, 1]
    "supporting_conversation_ids": array of conversation_ids
    "representative_snippet": short verbatim excerpt
    "reasoning":              one sentence
  }

Hard cap: 6 candidates. Prefer QUALITY over quantity. If nothing meets
the bar, return `{"candidates": []}`.

No markdown, no prose outside the JSON.
"""
