from django.db import models
from apps.common.models import BaseModel


class BusinessProfile(BaseModel):
    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='business_profiles'
    )
    name = models.CharField(max_length=255)
    service_desc = models.TextField(blank=True)
    policies = models.JSONField(default=dict, blank=True)
    pricing_notes = models.TextField(blank=True)
    hours = models.CharField(max_length=255, blank=True)
    coverage_area = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.name


class ScenarioTemplate(BaseModel):
    class Difficulty(models.TextChoices):
        EASY = 'easy', 'Easy'
        MEDIUM = 'medium', 'Medium'
        HARD = 'hard', 'Hard'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='scenario_templates'
    )
    name = models.CharField(max_length=255)
    system_prompt = models.TextField()
    difficulty = models.CharField(
        max_length=20, choices=Difficulty.choices, default=Difficulty.MEDIUM
    )
    intent = models.CharField(max_length=255, blank=True)
    rubric = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Script(BaseModel):
    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='scripts'
    )
    name = models.CharField(max_length=255)
    content = models.TextField()
    version = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.name} (v{self.version})"


class TrainingSession(BaseModel):
    class Status(models.TextChoices):
        CREATED = 'created', 'Created'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    org = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='training_sessions'
    )
    business_profile = models.ForeignKey(
        BusinessProfile, on_delete=models.SET_NULL, null=True, related_name='sessions'
    )
    scenario_template = models.ForeignKey(
        ScenarioTemplate, on_delete=models.SET_NULL, null=True, related_name='sessions'
    )
    script = models.ForeignKey(
        Script, on_delete=models.SET_NULL, null=True, related_name='sessions'
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CREATED
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Session {self.id} ({self.status})"


class SessionTurn(BaseModel):
    class Speaker(models.TextChoices):
        AI = 'ai', 'AI'
        CANDIDATE = 'candidate', 'Candidate'

    session = models.ForeignKey(
        TrainingSession, on_delete=models.CASCADE, related_name='turns'
    )
    speaker = models.CharField(max_length=20, choices=Speaker.choices)
    text = models.TextField()
    audio_url = models.CharField(max_length=500, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.speaker}: {self.text[:50]}"


class SessionResult(BaseModel):
    class Outcome(models.TextChoices):
        PASS = 'pass', 'Pass'
        REVIEW = 'review', 'Review'
        FAIL = 'fail', 'Fail'

    session = models.OneToOneField(
        TrainingSession, on_delete=models.CASCADE, related_name='result'
    )
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.REVIEW)
    signals = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)

    def __str__(self):
        return f"Result: {self.outcome} for session {self.session_id}"
