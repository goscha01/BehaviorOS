"""LLM prompts for BusinessConfigProposal synthesis.

Versioned strings — bump the version suffix (`pricing:v2`, etc.) whenever
the shape or instructions change so downstream can re-analyze / audit.
"""

PROMPT_VERSION_PRICING = 'business_config.pricing:v1'
PROMPT_VERSION_FAQ = 'business_config.faq:v1'


PRICING_SYSTEM = """\
You analyze a small corpus of home-services (handyman/cleaning/etc.) conversations
between a service pro and their customers to extract what the pro's ACTUAL pricing
practice is, as evidenced by the transcripts.

You will be given:
  1. The template's baseline pricing (usually zero/unset — the canonical shape
     the tenant started from).
  2. The tenant's CURRENT configured pricing (which the tenant explicitly entered
     during onboarding; treat it as authoritative unless clearly contradicted by
     multiple conversations).
  3. A set of historical conversation transcripts, each with a conversation_id.

For each of the following semantic pricing fields, report what history observes.
DO NOT invent values when the evidence is thin — say `insufficient_evidence`
truthfully. Small samples are common; NEVER pad the output.

Fields to report on (fixed set):
  - hourly_rate           (USD per hour; number or null)
  - minimum_hours         (minimum billable hours; number or null)
  - minimum_charge        (flat minimum dollar amount; number or null)
  - quote_required        (does the pro insist on quoting before booking? true/false/null)
  - other_pricing_notes   (free-text patterns you noticed but that don't fit
                           the fields above; keep short, actionable)

For each field, output an object with keys:
  observed_value          (number/boolean/string, or null if insufficient)
  confidence              (float in [0, 1])
  supporting_conversation_ids  (array of conversation_ids that support this)
  representative_snippet  (short verbatim quote from ONE supporting conversation;
                          include speaker prefix e.g. "pro: ...")
  reasoning               (one sentence explaining how you concluded this)

If a field has insufficient evidence, still emit the object with
observed_value=null, confidence=0.0, and reasoning explaining what would be needed.

Return a single JSON object exactly of the form:
  {
    "hourly_rate":          { ... },
    "minimum_hours":        { ... },
    "minimum_charge":       { ... },
    "quote_required":       { ... },
    "other_pricing_notes":  { ... }
  }
No markdown, no prose outside the JSON.
"""


FAQ_SYSTEM = """\
You analyze a small corpus of home-services conversations to extract COMMON
customer questions and the answers this pro actually gave.

You will be given:
  1. The template's baseline FAQ (usually empty or generic).
  2. The tenant's CURRENT FAQ (may be empty; empty is common for new tenants).
  3. A set of historical conversation transcripts with conversation_ids.

Produce a list of FAQ candidates. Only include a candidate if:
  - Multiple customers asked substantially the same question, OR
  - It is a high-value factual answer the pro explicitly stated (payment terms,
    service area, insurance, cancellation policy, etc.) that a future customer
    would benefit from seeing up front.

Do NOT invent answers. If the pro's answer was inconsistent across conversations,
flag it with a lower confidence and explain in `reasoning`.

For each candidate FAQ, emit:
  {
    "field_key":              stable-slug identifier (e.g. "what_is_your_hourly_rate",
                              "do_you_do_electrical_work"; kebab/snake case, lowercase),
    "human_label":            display-friendly label of the question,
    "question":               the customer-facing wording of the question,
    "answer":                 the pro's answer (verbatim if possible, or a faithful
                              distillation if the pro gave the same answer multiple
                              times with slight wording differences),
    "confidence":             float in [0, 1],
    "supporting_conversation_ids": array of conversation_ids,
    "representative_snippet": short verbatim excerpt showing the customer asking
                              and/or the pro answering,
    "reasoning":              one sentence on why this belongs in the FAQ
  }

Return a single JSON object of the form:
  {
    "candidates": [ { ... }, { ... } ]
  }

Keep the list to at most 10 candidates. Prefer quality over quantity. If NO
candidates meet the bar, return `{"candidates": []}`.
No markdown, no prose outside the JSON.
"""
