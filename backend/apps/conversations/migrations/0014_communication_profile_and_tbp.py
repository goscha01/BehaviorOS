# Migration: MVP CommunicationProfile v1 + TenantBehaviorProfile v1.
# Adds CommunicationProfileRun, CommunicationProfileDiff, TenantBehaviorProfile.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('conversations', '0013_unified_business_reconstruction'),
    ]

    operations = [
        migrations.CreateModel(
            name='CommunicationProfileRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant_external_id', models.CharField(blank=True, default='', max_length=128)),
                ('profile_version', models.CharField(max_length=64)),
                ('model', models.CharField(blank=True, default='', max_length=64)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('corpus_conversations', models.PositiveIntegerField(default=0)),
                ('agent_turns_scanned', models.PositiveIntegerField(default=0)),
                ('llm_calls', models.PositiveIntegerField(default=0)),
                ('llm_cost_usd', models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ('profile_json', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True, default='')),
                ('org', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='communication_profile_runs', to='accounts.organization')),
                ('corpus', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='communication_profile_runs', to='conversations.learningcorpus')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['corpus', 'status'], name='conv_comm_prof_corpus_status_idx'),
                    models.Index(fields=['tenant_external_id', '-created_at'], name='conv_comm_prof_tenant_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='CommunicationProfileDiff',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('dimension', models.CharField(max_length=64)),
                ('category', models.CharField(choices=[('SAME_AS_DEFAULT', 'Same as default'), ('DIFFERENT_FROM_DEFAULT', 'Different from default'), ('BUSINESS_SPECIFIC', 'Business-specific (no default)'), ('CONFLICTING_OR_UNCLEAR', 'Conflicting or unclear'), ('INSUFFICIENT_EVIDENCE', 'Insufficient evidence')], max_length=32)),
                ('default_value', models.JSONField(blank=True, default=dict)),
                ('observed_value', models.JSONField(blank=True, default=dict)),
                ('support_n', models.PositiveIntegerField(default=0)),
                ('confidence', models.CharField(blank=True, default='', max_length=16)),
                ('narrative', models.TextField(blank=True, default='')),
                ('proposed_override', models.JSONField(blank=True, default=dict)),
                ('evidence_conversation_ids', models.JSONField(blank=True, default=list)),
                ('review_state', models.CharField(choices=[('pending', 'Pending review'), ('accepted', 'Accepted by owner'), ('edited', 'Accepted with owner edit'), ('dismissed', 'Kept as default')], default='pending', max_length=16)),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('owner_edited_payload', models.JSONField(blank=True, null=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='diffs', to='conversations.communicationprofilerun')),
            ],
            options={
                'ordering': ['dimension'],
                'indexes': [
                    models.Index(fields=['run', 'category'], name='conv_comm_diff_cat_idx'),
                    models.Index(fields=['run', 'review_state'], name='conv_comm_diff_state_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=['run', 'dimension'], name='comm_profile_diff_dimension_unique'),
                ],
            },
        ),
        migrations.CreateModel(
            name='TenantBehaviorProfile',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant_external_id', models.CharField(max_length=128)),
                ('base_template_version', models.CharField(max_length=64)),
                ('profile_version', models.PositiveIntegerField()),
                ('business_rule_overrides', models.JSONField(blank=True, default=list)),
                ('custom_business_rules', models.JSONField(blank=True, default=list)),
                ('communication_overrides', models.JSONField(blank=True, default=list)),
                ('reconstruction_run_id', models.UUIDField(blank=True, null=True)),
                ('communication_profile_run_id', models.UUIDField(blank=True, null=True)),
                ('generated_at', models.DateTimeField()),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('org', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tenant_behavior_profiles', to='accounts.organization')),
            ],
            options={
                'ordering': ['-profile_version'],
                'indexes': [
                    models.Index(fields=['tenant_external_id', '-profile_version'], name='conv_tbp_tenant_ver_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=['tenant_external_id', 'profile_version'], name='tenant_behavior_profile_version_unique'),
                ],
            },
        ),
    ]
