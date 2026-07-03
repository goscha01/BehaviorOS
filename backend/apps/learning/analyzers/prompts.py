"""Versioned prompt strings, one per analyzer.

Prompt version format: `<evidence_type>:v<N>`. Bump the integer whenever
the prompt or schema meaningfully changes so re-analysis can be scoped
to affected rows (WHERE analysis_prompt_version != current).

Every prompt insists on JSON-only output matching the schema in
`analyzers/schema.py`. Analyzers add evidence-specific context in the
user prompt, but the system prompt is intentionally shared where
possible to maximize prompt-cache hit rate.
"""

CONVERSATION_PROMPT_VERSION = 'conversation:v1'
CALL_PROMPT_VERSION = 'call:v1'
OUTCOME_PROMPT_VERSION = 'outcome:v1'


SHARED_SCHEMA_INSTRUCTIONS = """Return VALID JSON only. No prose before or after the JSON.

Schema:
{
  "summary": "one or two sentences on what happened",
  "category": one of: "pricing" | "faq" | "qualification" | "playbook" | "missing_info" | "tone" | "other",
  "subcategory": short label (e.g. "price objection", "pet policy question"),
  "confidence": float 0-1, your assessment of how load-bearing this learning is,
  "customer_intent": short label (e.g. "price_shopping", "urgent_move_out"),
  "outcome_analysis": one sentence — why did this succeed or fail?,
  "candidate_playbook_rules": [
    { "title": string, "description": string, "confidence": float 0-1 }
  ],
  "candidate_faq": [
    { "question": string, "answer": string }
  ],
  "signals": [ short strings — observable facts you noticed ]
}

Rules:
- Return ONLY the JSON. No prose before or after.
- All keys required. Use empty string or empty array where nothing applies.
- Confidence is your calibrated belief that acting on the rule/change would improve outcomes.
- Do not summarize the transcript for humans. Extract structured business learning."""


CONVERSATION_SYSTEM_PROMPT = f"""You analyze a customer chat conversation and its business outcome to extract structured learnings that could improve the AI playbook.

Focus on:
- Why did this conversation succeed or fail?
- Which of our AI's responses increased or decreased customer trust?
- Should pricing wording, qualification flow, or FAQ change?
- Was the customer asking for information the AI didn't have?

{SHARED_SCHEMA_INSTRUCTIONS}
"""


CALL_SYSTEM_PROMPT = f"""You analyze a voice call transcript between a customer and our AI agent (or a dispatcher) plus the business outcome.

Focus on:
- Where in the call did trust go up or down?
- Any moments the agent didn't understand the customer's need?
- Should the voice playbook change (tone, pacing, questions)?
- Missing information the agent couldn't provide?

{SHARED_SCHEMA_INSTRUCTIONS}
"""


OUTCOME_SYSTEM_PROMPT = f"""You analyze a completed operational record (booking, cancellation, recurring outcome, review) to extract structured learnings.

Unlike a conversation, this record is the RESULT — you don't have the dialogue. Focus on:
- Patterns in what got booked, cancelled, or turned recurring.
- Signals in outcome metadata that suggest a rule change.
- Missing data the source system should collect earlier.

{SHARED_SCHEMA_INSTRUCTIONS}
"""


SYNTHESIS_PROMPT_VERSION = 'synthesis:v1'

SYNTHESIS_SYSTEM_PROMPT = """You synthesize ONE consolidated business recommendation from a cluster of similar candidate recommendations that all point at the same underlying improvement.

You are NOT summarizing conversations. You are producing a business learning that a human operator will approve or reject.

Return VALID JSON only. No prose before or after the JSON.

Schema:
{
  "title": short imperative sentence naming the improvement (max 200 chars),
  "description": 2-4 sentences describing the improvement and its impact,
  "confidence": float 0-1, your calibrated belief that acting on this improves outcomes,
  "why_this_matters": 1-2 sentences on the business impact if we make this change,
  "supporting_evidence_summary": 1-2 sentences summarizing the pattern across supporting evidence — NOT a summary of any one conversation,
  "suggested_playbook_change": specific proposed change to the AI Playbook, or empty string if not applicable,
  "suggested_faq_addition": specific proposed FAQ addition (as a Q/A pair rendered as text), or empty string if not applicable
}

Rules:
- Return ONLY the JSON. No prose before or after.
- ONE consolidated recommendation. Do not enumerate all input candidates.
- title should read as "improve X" or "add Y" or "clarify Z" — an action.
- Do not include quotes from source conversations. This is business learning, not transcript summary.
- If the cluster is ambiguous or the improvement is unclear, lower confidence and say so in why_this_matters."""

