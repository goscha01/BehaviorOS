from django.utils import timezone

from apps.training.models import TrainingSession, SessionTurn
from apps.training.services.conversation import generate_reply
from apps.training.services.elevenlabs import generate_speech


def start_session(session):
    """Start a training session: set status to running and generate first AI message."""
    session.status = TrainingSession.Status.RUNNING
    session.started_at = timezone.now()
    session.save()

    ai_text = generate_reply(session)

    audio_url = generate_speech(ai_text)

    turn = SessionTurn.objects.create(
        session=session,
        speaker=SessionTurn.Speaker.AI,
        text=ai_text,
        audio_url=audio_url,
    )
    return turn


def process_turn(session, candidate_text):
    """Process a candidate's turn: save their message, generate AI response."""
    SessionTurn.objects.create(
        session=session,
        speaker=SessionTurn.Speaker.CANDIDATE,
        text=candidate_text,
    )

    ai_text = generate_reply(session, candidate_text)

    audio_url = generate_speech(ai_text)

    ai_turn = SessionTurn.objects.create(
        session=session,
        speaker=SessionTurn.Speaker.AI,
        text=ai_text,
        audio_url=audio_url,
    )
    return ai_turn


def complete_session(session):
    """Mark a session as completed and trigger evaluation."""
    session.status = TrainingSession.Status.COMPLETED
    session.ended_at = timezone.now()
    session.save()

    from apps.training.tasks import extract_session_signals
    extract_session_signals.delay(str(session.id))
