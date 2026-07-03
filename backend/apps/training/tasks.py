from celery import shared_task

from apps.training.models import TrainingSession


@shared_task
def extract_session_signals(session_id):
    """Async task to extract signals and outcome from a completed session."""
    from apps.training.services.signals_extractor import extract_signals_and_outcome

    try:
        session = TrainingSession.objects.get(id=session_id)
    except TrainingSession.DoesNotExist:
        return

    if session.status != TrainingSession.Status.COMPLETED:
        return

    extract_signals_and_outcome(session)
