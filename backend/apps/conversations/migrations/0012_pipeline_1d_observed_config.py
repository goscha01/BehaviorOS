# Migration: Pipeline 1D — Observed Business Configuration Extraction.
#
# Adds four models:
#   - ObservedBusinessFact: normalized facts from conversation evidence
#   - ConfiguredBusinessFact: normalized facts from LB TenantConfigSnapshot
#   - ObservedFactExtractionRun: one extractor run per (org, corpus, domain)
#   - ConfiguredFactParserRun: one parser run per (snapshot, domain)
#   - OntologyReviewCandidate: evidence for a future extractor reliability pass

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0001_initial'),
        ('conversations', '0011_rom_v1_maturity_semantic'),
    ]

    operations = [
        migrations.CreateModel(
            name='ObservedFactExtractionRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.CharField(choices=[('pricing', 'Pricing / quoted amounts'), ('qualification', 'Qualification questions'), ('faq', 'FAQ / customer question topics'), ('service_scope', 'Service scope and boundaries')], max_length=32)),
                ('extractor_version', models.CharField(max_length=64)),
                ('model', models.CharField(blank=True, default='', max_length=64)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('conversations_processed', models.PositiveIntegerField(default=0)),
                ('facts_emitted', models.PositiveIntegerField(default=0)),
                ('ontology_review_candidates_emitted', models.PositiveIntegerField(default=0)),
                ('llm_input_tokens', models.PositiveIntegerField(default=0)),
                ('llm_output_tokens', models.PositiveIntegerField(default=0)),
                ('llm_cost_usd', models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ('stats_json', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True, default='')),
                ('org', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='observed_fact_extraction_runs', to='accounts.organization')),
                ('corpus', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='observed_fact_extraction_runs', to='conversations.learningcorpus')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['org', 'domain', '-created_at'], name='conv_ofr_org_domain_idx'),
                    models.Index(fields=['corpus', 'domain', '-created_at'], name='conv_ofr_corpus_domain_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ConfiguredFactParserRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.CharField(choices=[('pricing', 'Pricing / quoted amounts'), ('qualification', 'Qualification questions'), ('faq', 'FAQ / customer question topics'), ('service_scope', 'Service scope and boundaries')], max_length=32)),
                ('parser_version', models.CharField(max_length=64)),
                ('model', models.CharField(blank=True, default='', max_length=64)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('facts_emitted', models.PositiveIntegerField(default=0)),
                ('llm_input_tokens', models.PositiveIntegerField(default=0)),
                ('llm_output_tokens', models.PositiveIntegerField(default=0)),
                ('llm_cost_usd', models.DecimalField(decimal_places=4, default=0, max_digits=10)),
                ('stats_json', models.JSONField(blank=True, default=dict)),
                ('error_message', models.TextField(blank=True, default='')),
                ('snapshot', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='configured_fact_parser_runs', to='conversations.tenantconfigsnapshot')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['snapshot', 'domain', '-created_at'], name='conv_cfp_snap_domain_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ObservedBusinessFact',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.CharField(choices=[('pricing', 'Pricing / quoted amounts'), ('qualification', 'Qualification questions'), ('faq', 'FAQ / customer question topics'), ('service_scope', 'Service scope and boundaries')], max_length=32)),
                ('fact_type', models.CharField(help_text='Domain-specific token', max_length=64)),
                ('subject_key_json', models.JSONField(default=dict)),
                ('subject_key_dimensions', models.JSONField(default=list)),
                ('subject_key_hash', models.CharField(db_index=True, max_length=64)),
                ('value_json', models.JSONField(default=dict)),
                ('support_n', models.PositiveIntegerField(default=0)),
                ('aggregate_confidence', models.FloatField(default=0.0)),
                ('evidence_conversation_ids', models.JSONField(blank=True, default=list)),
                ('evidence_turn_ids', models.JSONField(blank=True, default=list)),
                ('first_seen_at', models.DateTimeField(blank=True, null=True)),
                ('last_seen_at', models.DateTimeField(blank=True, null=True)),
                ('org', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='observed_business_facts', to='accounts.organization')),
                ('corpus', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='observed_business_facts', to='conversations.learningcorpus')),
                ('extraction_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='facts', to='conversations.observedfactextractionrun')),
            ],
            options={
                'ordering': ['-created_at'],
                'constraints': [
                    models.UniqueConstraint(fields=['extraction_run', 'domain', 'fact_type', 'subject_key_hash'], name='observed_business_fact_unique'),
                ],
                'indexes': [
                    models.Index(fields=['org', 'domain'], name='conv_obf_org_domain_idx'),
                    models.Index(fields=['extraction_run', 'domain'], name='conv_obf_run_domain_idx'),
                    models.Index(fields=['domain', 'fact_type'], name='conv_obf_domain_type_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ConfiguredBusinessFact',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('domain', models.CharField(choices=[('pricing', 'Pricing / quoted amounts'), ('qualification', 'Qualification questions'), ('faq', 'FAQ / customer question topics'), ('service_scope', 'Service scope and boundaries')], max_length=32)),
                ('fact_type', models.CharField(max_length=64)),
                ('subject_key_json', models.JSONField(default=dict)),
                ('subject_key_dimensions', models.JSONField(default=list)),
                ('subject_key_hash', models.CharField(db_index=True, max_length=64)),
                ('value_json', models.JSONField(default=dict)),
                ('source_pointer', models.JSONField(blank=True, default=dict)),
                ('parser_confidence', models.FloatField(default=1.0)),
                ('snapshot', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='configured_business_facts', to='conversations.tenantconfigsnapshot')),
                ('parser_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='facts', to='conversations.configuredfactparserrun')),
            ],
            options={
                'ordering': ['-created_at'],
                'constraints': [
                    models.UniqueConstraint(fields=['parser_run', 'domain', 'fact_type', 'subject_key_hash'], name='configured_business_fact_unique'),
                ],
                'indexes': [
                    models.Index(fields=['snapshot', 'domain'], name='conv_cbf_snap_domain_idx'),
                    models.Index(fields=['parser_run', 'domain'], name='conv_cbf_run_domain_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='OntologyReviewCandidate',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('kind', models.CharField(choices=[('event_mis_classified', 'Event type appears misapplied to this evidence'), ('other_cluster', 'FAQ/qualification cluster labeled OTHER; may need taxonomy expansion')], max_length=32)),
                ('original_event_type', models.CharField(blank=True, default='', max_length=64)),
                ('proposed_scope', models.CharField(blank=True, default='', max_length=64)),
                ('proposed_topic', models.CharField(blank=True, default='', max_length=128)),
                ('evidence_conversation_id', models.CharField(blank=True, max_length=64)),
                ('evidence_turn_id', models.CharField(blank=True, max_length=64)),
                ('evidence_text', models.CharField(blank=True, max_length=1000)),
                ('confidence', models.FloatField(default=0.0)),
                ('reviewed', models.BooleanField(default=False)),
                ('org', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ontology_review_candidates', to='accounts.organization')),
                ('extraction_run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ontology_review_candidates', to='conversations.observedfactextractionrun')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['org', 'kind', 'reviewed'], name='conv_orc_org_kind_idx'),
                    models.Index(fields=['extraction_run', 'kind'], name='conv_orc_run_kind_idx'),
                ],
            },
        ),
    ]
