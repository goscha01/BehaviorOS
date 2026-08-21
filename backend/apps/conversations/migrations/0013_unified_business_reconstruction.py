# Migration: 1D Hardening — Unified Business Reconstruction.
# Adds UnifiedBusinessReconstructionRun + ReconstructedBusinessFact.

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('conversations', '0012_pipeline_1d_observed_config'),
    ]

    operations = [
        migrations.CreateModel(
            name='UnifiedBusinessReconstructionRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tenant_external_id', models.CharField(max_length=128)),
                ('reconstruction_version', models.CharField(max_length=64)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('facts_emitted', models.PositiveIntegerField(default=0)),
                ('stats_json', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True, default='')),
                ('org', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reconstruction_runs', to='accounts.organization')),
                ('snapshot', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='reconstruction_runs', to='conversations.tenantconfigsnapshot')),
                ('input_pricing_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.observedfactextractionrun')),
                ('input_qualification_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.observedfactextractionrun')),
                ('input_faq_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.observedfactextractionrun')),
                ('input_service_scope_run', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.observedfactextractionrun')),
                ('input_pricing_parser', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.configuredfactparserrun')),
                ('input_qualification_parser', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.configuredfactparserrun')),
                ('input_faq_parser', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.configuredfactparserrun')),
                ('input_service_scope_parser', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='conversations.configuredfactparserrun')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['org', 'tenant_external_id', '-created_at'], name='conv_recon_org_tenant_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ReconstructedBusinessFact',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.CharField(max_length=32)),
                ('canonical_subject_json', models.JSONField(default=dict)),
                ('canonical_subject_hash', models.CharField(db_index=True, max_length=64)),
                ('observed_value_json', models.JSONField(blank=True, default=dict)),
                ('context_json', models.JSONField(blank=True, default=dict)),
                ('configured_equivalent_json', models.JSONField(blank=True, default=dict)),
                ('support_n', models.PositiveIntegerField(default=0)),
                ('aggregate_confidence', models.FloatField(default=0.0)),
                ('consistency', models.CharField(choices=[('consistent', 'Consistent across support'), ('contradictory', 'Contradictory across support'), ('context_dependent', 'Varies by context'), ('undetermined', 'Not enough evidence')], default='undetermined', max_length=32)),
                ('relationship_to_config', models.CharField(choices=[('CONFIRMED_BY_BEHAVIOR', 'Configured rule + observed evidence supports it'), ('OBSERVED_NOT_CONFIGURED', 'Real observed rule with no configured entry'), ('CONFIGURED_NOT_OBSERVED', 'Configured rule with no supporting observation'), ('CONTRADICTORY_OBSERVED_BEHAVIOR', 'Agents contradict each other on the same rule'), ('CONTEXT_DEPENDENT', 'Rule varies by service/frequency/condition/promo'), ('LIKELY_LEAD_SOURCE_PROVIDED', 'Configured field likely pre-populated by Thumbtack/Yelp'), ('ONTOLOGY_OR_EXTRACTION_ISSUE', 'Fact has a known extraction / ontology quality issue'), ('INSUFFICIENT_EVIDENCE', 'Not enough support to classify either way')], max_length=48)),
                ('quality_flags', models.JSONField(blank=True, default=list)),
                ('onboarding_class', models.CharField(choices=[('SAFE_TO_PROPOSE', 'High support + consistent + no quality flags'), ('NEEDS_OWNER_CONFIRMATION', 'Contradictory, context-dependent, or medium quality'), ('DO_NOT_PROPOSE', 'Low support / malformed / extraction issue')], max_length=32)),
                ('onboarding_rationale', models.TextField(blank=True, default='')),
                ('evidence_conversation_ids', models.JSONField(blank=True, default=list)),
                ('evidence_turn_ids', models.JSONField(blank=True, default=list)),
                ('source_observed_fact_ids', models.JSONField(blank=True, default=list)),
                ('source_configured_fact_ids', models.JSONField(blank=True, default=list)),
                ('reconstruction_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='facts', to='conversations.unifiedbusinessreconstructionrun')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['reconstruction_run', 'domain'], name='conv_rf_run_domain_idx'),
                    models.Index(fields=['reconstruction_run', 'relationship_to_config'], name='conv_rf_run_relationship_idx'),
                    models.Index(fields=['reconstruction_run', 'onboarding_class'], name='conv_rf_run_onboarding_idx'),
                ],
            },
        ),
    ]
