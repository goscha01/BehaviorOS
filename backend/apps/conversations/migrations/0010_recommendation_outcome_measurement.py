# Migration for the RecommendationOutcomeMeasurement model (ROM v1
# Step 4). Freezes the experimental contract per applied recommendation.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('conversations', '0009_recommendation_proposal'),
    ]

    operations = [
        migrations.CreateModel(
            name='RecommendationOutcomeMeasurement',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),

                ('lb_recommendation_application_id', models.CharField(max_length=64)),
                ('tenant_external_id', models.CharField(max_length=128)),

                ('measurement_spec_key', models.CharField(max_length=64)),
                ('measurement_spec_version', models.CharField(max_length=32)),
                ('frozen_spec_json', models.JSONField()),

                ('applied_at', models.DateTimeField()),
                ('subject_state', models.CharField(blank=True, default='', max_length=32)),
                ('subject_signals', models.JSONField(blank=True, default=list)),
                ('target_signal', models.CharField(help_text='The specific signal from subject_signals that cohort_entry was instantiated with', max_length=64)),

                ('pre_effective_config_hash', models.CharField(blank=True, default='', help_text='LB config hash BEFORE the apply was written', max_length=64)),
                ('treatment_effective_config_hash', models.CharField(help_text='LB config hash AFTER the apply was written; defines the "clean" post cohort membership', max_length=64)),
                ('treatment_managed_hash', models.CharField(help_text='LB behavior_os_managed subtree hash after apply; used to distinguish treatment-changed vs environment-contamination on post conversations', max_length=64)),
                ('effective_config_schema_version', models.CharField(help_text='Producer schema version, e.g. lb-effective-config-v1; evaluator refuses to cross-compare mismatched versions', max_length=32)),

                ('pre_cohort_conversation_ids', models.JSONField(blank=True, default=list, help_text='Immutable list of Conversation ids that matched cohort_entry within the baseline_window_days lookback from applied_at')),
                ('pre_cohort_frozen_at', models.DateTimeField(blank=True, null=True)),
                ('pre_n', models.PositiveIntegerField(default=0, help_text='Baseline arm size — resolved outcomes only (positive + negative, unresolved excluded)')),
                ('pre_positive_n', models.PositiveIntegerField(default=0)),
                ('pre_rate', models.FloatField(blank=True, null=True, help_text='pre_positive_n / pre_n. Null when pre_n == 0.')),

                ('post_n', models.PositiveIntegerField(default=0)),
                ('post_positive_n', models.PositiveIntegerField(default=0)),
                ('post_rate', models.FloatField(blank=True, null=True)),

                ('target_signal_conversations_n', models.PositiveIntegerField(default=0)),
                ('provenance_eligible_n', models.PositiveIntegerField(default=0)),
                ('provenance_pending_n', models.PositiveIntegerField(default=0)),
                ('provenance_hash_failed_n', models.PositiveIntegerField(default=0)),
                ('provenance_schema_mismatch_n', models.PositiveIntegerField(default=0)),
                ('contaminated_n', models.PositiveIntegerField(default=0, help_text='post conversations with matching managed hash but differing full hash — environment contamination')),
                ('treatment_moved_n', models.PositiveIntegerField(default=0, help_text='post conversations whose managed hash differs from treatment — the BehaviorOS-managed rule itself changed after apply, so these belong to a different measurement cohort')),

                ('effect_size_pp', models.FloatField(blank=True, null=True, help_text='(post_rate - pre_rate) in percentage points')),
                ('ci_low_pp', models.FloatField(blank=True, null=True)),
                ('ci_high_pp', models.FloatField(blank=True, null=True)),
                ('p_value', models.FloatField(blank=True, null=True)),

                ('status', models.CharField(choices=[
                    ('baseline_frozen', 'Baseline frozen — awaiting post-observations'),
                    ('collecting', 'Collecting post-treatment observations'),
                    ('ready', 'Min observation thresholds met — terminal eval can be attempted'),
                    ('improved', 'Terminal — improved'),
                    ('no_material_change', 'Terminal — no material change'),
                    ('worse', 'Terminal — worse'),
                    ('inconclusive', 'Terminal — inconclusive'),
                ], default='baseline_frozen', max_length=32)),
                ('status_reason', models.CharField(blank=True, default='', help_text='One-line explanation of why status is what it is (e.g. "sample_below_floor", "provenance_coverage_below_floor", "improved: +12.5pp p=0.03")', max_length=255)),
                ('evaluation_version', models.CharField(blank=True, default='', help_text='Version tag of the evaluator that produced the current status/verdict — bumps when scoring logic changes so historical rows are traceable', max_length=32)),

                ('measurement_started_at', models.DateTimeField()),
                ('measurement_deadline_at', models.DateTimeField(help_text='applied_at + max_window_days_for_inconclusive; evaluator transitions to INCONCLUSIVE at deadline if no other terminal has fired')),
                ('last_evaluated_at', models.DateTimeField(blank=True, null=True)),
                ('finalized_at', models.DateTimeField(blank=True, null=True, help_text='Set when status enters a TERMINAL_STATUSES value; evaluator refuses to update the row after this')),

                ('org', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recommendation_outcome_measurements', to='accounts.organization')),
                ('recommendation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='outcome_measurements', to='conversations.behaviorrecommendation')),
            ],
            options={
                'ordering': ['-created_at'],
                'constraints': [
                    models.UniqueConstraint(fields=['lb_recommendation_application_id'], name='rom_v1_lb_app_unique'),
                ],
                'indexes': [
                    models.Index(fields=['org', 'status'], name='conv_rom_org_status_idx'),
                    models.Index(fields=['tenant_external_id', 'status'], name='conv_rom_tenant_status_idx'),
                    models.Index(fields=['recommendation'], name='conv_rom_recommendation_idx'),
                    models.Index(fields=['measurement_deadline_at'], name='conv_rom_deadline_idx'),
                ],
            },
        ),
    ]
