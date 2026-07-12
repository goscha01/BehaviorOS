"""Create tables for models whose migration state doesn't match the DB.

Backstop for the Phase 1 bootstrap: `makemigrations` regenerates a
single 0001_initial per app on every deploy (no migrations checked in),
so once the DB has learning.0001_initial marked applied, ADDING a new
model to the learning app never actually creates its table — `migrate`
sees the migration name already applied and moves on.

This command idempotently creates any missing table for models in the
learning app. Safe to run every deploy. Remove once real migration
files are checked in.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = 'Idempotently create tables for learning app models missing from the DB.'

    def handle(self, *args, **options):
        existing = set(connection.introspection.table_names())
        created = []
        for model in django_apps.get_app_config('learning').get_models():
            table = model._meta.db_table
            if table in existing:
                continue
            with connection.schema_editor() as schema_editor:
                schema_editor.create_model(model)
            created.append(table)
        if created:
            self.stdout.write(self.style.SUCCESS(
                f'ensure_schema: created tables={created}'
            ))
        else:
            self.stdout.write('ensure_schema: all learning tables exist')
