"""Dual-write pricing verdict values on ReconstructedBusinessFact.

Adds four pricing-specific choices to
`ReconstructedBusinessFact.relationship_to_config` alongside the
existing generic values. Only the pricing pipeline emits the new
values; qualification / faq / service_scope continue using the
legacy ones untouched.

Choices-only change → no data migration, no default backfill.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0014_communication_profile_and_tbp'),
    ]

    operations = [
        migrations.AlterField(
            model_name='reconstructedbusinessfact',
            name='relationship_to_config',
            field=models.CharField(
                choices=[
                    ('CONFIRMED_BY_BEHAVIOR',
                     'Configured rule + observed evidence supports it'),
                    ('OBSERVED_NOT_CONFIGURED',
                     'Real observed rule with no configured entry'),
                    ('CONFIGURED_NOT_OBSERVED',
                     'Configured rule with no supporting observation'),
                    ('CONTRADICTORY_OBSERVED_BEHAVIOR',
                     'Agents contradict each other on the same rule'),
                    ('CONTEXT_DEPENDENT',
                     'Rule varies by service/frequency/condition/promo'),
                    ('LIKELY_LEAD_SOURCE_PROVIDED',
                     'Configured field likely pre-populated by Thumbtack/Yelp'),
                    ('ONTOLOGY_OR_EXTRACTION_ISSUE',
                     'Fact has a known extraction / ontology quality issue'),
                    ('INSUFFICIENT_EVIDENCE',
                     'Not enough support to classify either way'),
                    ('MATCH',
                     'Observed quote matches the compatible configured rule'),
                    ('DIFFERS_FROM_CONFIG',
                     "Observed quote is outside the compatible configured rule's tolerance"),
                    ('INSUFFICIENT_CONTEXT_TO_COMPARE',
                     'Configured candidates exist but observed context lacks the dimensions to choose'),
                    ('VARIABLE_CONTEXT_DEPENDENT',
                     'Observed price distribution is too heterogeneous to compare'),
                ],
                max_length=48,
            ),
        ),
    ]
