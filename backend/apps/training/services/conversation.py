from openai import OpenAI
from django.conf import settings


def build_system_prompt(session):
    """Build the system prompt for the AI customer persona."""
    scenario = session.scenario_template
    profile = session.business_profile
    script = session.script

    parts = []

    if scenario:
        parts.append(scenario.system_prompt)

    if profile:
        parts.append(
            f"\nBusiness Context:\n"
            f"Company: {profile.name}\n"
            f"Services: {profile.service_desc}\n"
            f"Coverage Area: {profile.coverage_area}\n"
            f"Hours: {profile.hours}\n"
            f"Pricing: {profile.pricing_notes}\n"
        )
        if profile.policies:
            parts.append(f"Policies: {profile.policies}")

    if script:
        parts.append(f"\nTraining Script Reference:\n{script.content}")

    if not parts:
        parts.append(
            "You are a customer calling a service dispatch company. "
            "Act as a realistic customer with a service need. "
            "Be conversational, ask questions, and respond naturally."
        )

    return '\n\n'.join(parts)


def generate_reply(session, candidate_message=None):
    """Generate the AI customer's next response using OpenAI."""
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    system_prompt = build_system_prompt(session)

    messages = [{'role': 'system', 'content': system_prompt}]

    turns = session.turns.order_by('created_at')
    for turn in turns:
        role = 'assistant' if turn.speaker == 'ai' else 'user'
        messages.append({'role': role, 'content': turn.text})

    if candidate_message:
        messages.append({'role': 'user', 'content': candidate_message})

    response = client.chat.completions.create(
        model='gpt-4o-mini',
        messages=messages,
        max_tokens=300,
        temperature=0.8,
    )

    return response.choices[0].message.content
