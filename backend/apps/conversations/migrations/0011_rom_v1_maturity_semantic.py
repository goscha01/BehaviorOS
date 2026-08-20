# Migration: ROM v1 attribution semantic correction — add matured /
# unresolved / negative counters on the measurement row.
#
# Corresponds to the outcome_semantics=terminal_known_after_maturity_v1
# fix. Existing measurement rows retain zeros for the new columns,
# which is safe: they were scored under the old (14-day captured_at)
# rule and won't be re-scored (the frozen_spec_json on each row is
# still the v1 spec, so re-running the evaluator picks up the new
# semantic — but existing rows only had the synthetic benchmark row,
# no real production rows exist yet).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('conversations', '0010_recommendation_outcome_measurement'),
    ]

    operations = [
        migrations.AddField(
            model_name='recommendationoutcomemeasurement',
            name='pre_matured_n',
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    'Baseline cohort members that have finished '
                    'maturing (started_at + attribution_window_days '
                    '<= pre_cohort_frozen_at)'
                ),
            ),
        ),
        migrations.AddField(
            model_name='recommendationoutcomemeasurement',
            name='pre_negative_n',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='recommendationoutcomemeasurement',
            name='pre_unresolved_n',
            field=models.PositiveIntegerField(
                default=0,
                help_text=(
                    'Matured baseline members whose latest '
                    'OutcomeSnapshot showed no terminal'
                ),
            ),
        ),
        migrations.AddField(
            model_name='recommendationoutcomemeasurement',
            name='post_matured_n',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='recommendationoutcomemeasurement',
            name='post_negative_n',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='recommendationoutcomemeasurement',
            name='post_unresolved_n',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
