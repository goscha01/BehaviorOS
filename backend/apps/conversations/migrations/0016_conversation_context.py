"""Canonical Conversation Context — one-to-one cache per Conversation.

Backs `apps.conversations.context`. The row persists the resolved
canonical attributes, all raw observations (winners + losers),
conflict reports, and the source-version fingerprint used for
cache invalidation.

No data migration — new table only.
"""

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0015_pricing_verdict_dualwrite'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConversationContext',
            fields=[
                (
                    'id',
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('resolved_at', models.DateTimeField()),
                (
                    'source_versions_json',
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    'attributes_json',
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    'observations_json',
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    'conflicts_json',
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    'coverage_json',
                    models.JSONField(blank=True, default=dict),
                ),
                (
                    'conversation',
                    models.OneToOneField(
                        on_delete=models.deletion.CASCADE,
                        related_name='canonical_context',
                        to='conversations.conversation',
                    ),
                ),
            ],
            options={
                'ordering': ['-resolved_at'],
                'indexes': [
                    models.Index(
                        fields=['-resolved_at'],
                        name='conv_ctx_resolved_at_idx',
                    ),
                ],
            },
        ),
    ]
