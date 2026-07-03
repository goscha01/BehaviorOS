from openai import OpenAI
from django.conf import settings

from apps.training.models import SessionResult


def extract_signals_and_outcome(session):
    """Analyze a completed session transcript and extract structured signals + outcome."""
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    turns = session.turns.order_by('created_at')
    transcript = '\n'.join(
        f"{'AI Customer' if t.speaker == 'ai' else 'Dispatcher'}: {t.text}"
        for t in turns
    )

    rubric = {}
    if session.scenario_template:
        rubric = session.scenario_template.rubric or {}

    rubric_text = ''
    if rubric:
        rubric_text = f"\n\nEvaluation Rubric:\n{rubric}"

    prompt = f"""Analyze the following dispatcher training session transcript and provide a structured evaluation.

Transcript:
{transcript}
{rubric_text}

Respond in this exact JSON format:
{{
    "signals": {{
        "confirmed_address": true/false,
        "confirmed_price": true/false,
        "confirmed_schedule": true/false,
        "followed_script": true/false,
        "professional_tone": true/false,
        "handled_objections": true/false
    }},
    "flags": [],
    "outcome": "pass" or "review" or "fail",
    "notes": "Brief summary of the dispatcher's performance"
}}

Only respond with valid JSON, nothing else."""

    response = client.chat.completions.create(
        model='gpt-4o-mini',
        messages=[{'role': 'user', 'content': prompt}],
        max_tokens=500,
        temperature=0.3,
    )

    import json
    try:
        result_data = json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, IndexError):
        result_data = {
            'signals': {},
            'flags': [],
            'outcome': 'review',
            'notes': 'Could not parse evaluation result.',
        }

    result, _ = SessionResult.objects.update_or_create(
        session=session,
        defaults={
            'outcome': result_data.get('outcome', 'review'),
            'signals': {
                'signals': result_data.get('signals', {}),
                'flags': result_data.get('flags', []),
            },
            'notes': result_data.get('notes', ''),
        },
    )
    return result
