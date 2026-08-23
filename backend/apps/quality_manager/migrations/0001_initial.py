"""QM V1 — initial schema.

Two tables: QualityRun (per-invocation) + QualityEvaluation
(per (run, conversation | corpus, dimension, subject) tuple).

QualityEvaluation.conversation is nullable so corpus-level pattern
findings live in the same table as per-conversation drill-down rows.
"""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('accounts', '0001_initial'),
        ('conversations', '0016_conversation_context'),
    ]

    operations = [
        migrations.CreateModel(
            name='QualityRun',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4, editable=False,
                        primary_key=True, serialize=False,
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('qm_version', models.CharField(default='qm-v1', max_length=64)),
                (
                    'dimensions_enabled_json',
                    models.JSONField(blank=True, default=list),
                ),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('pending', 'Pending'),
                            ('running', 'Running'),
                            ('completed', 'Completed'),
                            ('failed', 'Failed'),
                        ],
                        default='pending', max_length=16,
                    ),
                ),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('stats_json', models.JSONField(blank=True, default=dict)),
                (
                    'conversations_evaluated',
                    models.PositiveIntegerField(default=0),
                ),
                ('error_message', models.TextField(blank=True, default='')),
                (
                    'org',
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name='quality_runs',
                        to='accounts.organization',
                    ),
                ),
                (
                    'reconstruction_run',
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name='quality_runs',
                        to='conversations.unifiedbusinessreconstructionrun',
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(
                        fields=['org', '-created_at'],
                        name='qm_run_org_created_idx',
                    ),
                    models.Index(
                        fields=['status'], name='qm_run_status_idx',
                    ),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=['org', 'reconstruction_run', 'qm_version'],
                        name='qm_run_org_recon_version_unique',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='QualityEvaluation',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4, editable=False,
                        primary_key=True, serialize=False,
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('dimension', models.CharField(max_length=64)),
                ('subject_key_json', models.JSONField(blank=True, default=dict)),
                (
                    'state',
                    models.CharField(
                        choices=[
                            ('PASS', 'Pass'),
                            ('FAIL', 'Fail'),
                            ('UNKNOWN_NOT_EVALUABLE', 'Unknown / not evaluable'),
                            ('NOT_APPLICABLE', 'Not applicable'),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    'severity',
                    models.CharField(
                        blank=True, default='',
                        choices=[
                            ('info', 'Info'),
                            ('warning', 'Warning'),
                            ('critical', 'Critical'),
                        ],
                        max_length=16,
                    ),
                ),
                ('reason_code', models.CharField(blank=True, default='', max_length=64)),
                ('rationale_text', models.TextField(blank=True, default='')),
                ('evidence_json', models.JSONField(blank=True, default=list)),
                (
                    'source_reconstructed_fact_id',
                    models.UUIDField(blank=True, null=True),
                ),
                (
                    'conversation',
                    models.ForeignKey(
                        blank=True, null=True,
                        on_delete=models.deletion.CASCADE,
                        related_name='quality_evaluations',
                        to='conversations.conversation',
                    ),
                ),
                (
                    'run',
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name='evaluations',
                        to='quality_manager.qualityrun',
                    ),
                ),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(
                        fields=['run', 'dimension', 'state'],
                        name='qm_eval_run_dim_state_idx',
                    ),
                    models.Index(
                        fields=['conversation', 'dimension'],
                        name='qm_eval_conv_dim_idx',
                    ),
                    models.Index(
                        fields=['run', 'state', 'severity'],
                        name='qm_eval_run_state_sev_idx',
                    ),
                ],
            },
        ),
    ]
