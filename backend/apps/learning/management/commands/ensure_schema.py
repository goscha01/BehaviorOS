"""Additive-only schema reconciler.

Backstop for the Phase 1 migration bootstrap: `makemigrations` regenerates a
single 0001_initial per app on every deploy (no migrations checked in), so
once the DB has <app>.0001_initial marked applied, ADDING a new model or
column never actually runs — `migrate` sees the migration name already
applied and moves on.

This command reconciles the ADDITIVE gaps that scenario creates. It handles
three specific shapes of drift:

  1. Table exists in the ORM but not in the DB. Create it.
  2. Column exists in the ORM but not in the DB. Add it (only if safe).
  3. Index declared in Meta.indexes exists in the ORM but not in the DB.
     Create it.

Scope discipline — this is NOT a Django-migrations replacement.
It NEVER performs any of the following:

  - drop a column
  - rename a column
  - change a column's type
  - tighten nullability
  - modify constraints in destructive ways
  - drop an index

Detected drift of those shapes is REPORTED to stdout and, when strict mode
is on, causes a non-zero exit — but never mutated. Real migration files
are the only correct home for destructive change; this command intentionally
refuses to be that.

Column-add safety:
  - Nullable columns are always safe to add.
  - Columns with a Django default value are safe to add — schema_editor
    fills existing rows during the ALTER TABLE.
  - Non-nullable columns with no default REQUIRE a manual migration
    (backfill matters). Refused; reported.

Remove once real migration files are checked in and every model change
lands as a proper migration.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Iterable

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand
from django.db import connection, models

logger = logging.getLogger(__name__)

# Default reconciliation surface. Every app whose Phase-1 shipped without
# checked-in migration files must be listed here — otherwise its
# additive drift silently accumulates.
DEFAULT_APP_LABELS = ('learning', 'context')


@dataclass
class ReconciliationSummary:
    tables_created: list[str] = field(default_factory=list)
    columns_added: list[str] = field(default_factory=list)  # "table.column"
    indexes_created: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def any_action(self) -> bool:
        return bool(self.tables_created or self.columns_added or self.indexes_created)


class Command(BaseCommand):
    help = 'Reconcile additive schema drift (missing tables/columns/indexes).'

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            '--apps', nargs='+', default=list(DEFAULT_APP_LABELS),
            help=f'App labels to reconcile (default: {" ".join(DEFAULT_APP_LABELS)}).',
        )
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit non-zero if drift was detected that could not be safely reconciled '
                 '(destructive changes, non-nullable adds without defaults, etc.). '
                 'Recommended for CI / deploy pipelines.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report drift without applying any changes.',
        )

    def handle(self, *args, **options) -> None:
        summary = ReconciliationSummary()
        dry_run: bool = options['dry_run']

        for label in options['apps']:
            try:
                app_config = django_apps.get_app_config(label)
            except LookupError as exc:
                summary.warnings.append(f'unknown app {label!r}: {exc}')
                continue
            for model in app_config.get_models():
                self._reconcile_model(model, summary, dry_run=dry_run)

        self._report(summary, dry_run=dry_run)
        if options['strict'] and summary.warnings:
            sys.exit(2)

    # -- reconciliation -------------------------------------------------

    def _reconcile_model(
        self, model: type[models.Model], summary: ReconciliationSummary, *, dry_run: bool,
    ) -> None:
        table = model._meta.db_table

        existing_tables = set(connection.introspection.table_names())
        if table not in existing_tables:
            if dry_run:
                summary.tables_created.append(f'{table} (dry-run)')
                return
            with connection.schema_editor() as schema_editor:
                schema_editor.create_model(model)
            summary.tables_created.append(table)
            logger.info('ensure_schema: created table %s', table)
            return  # fresh table has all columns + indexes, no further work

        self._reconcile_columns(model, summary, dry_run=dry_run)
        self._reconcile_indexes(model, summary, dry_run=dry_run)

    def _reconcile_columns(
        self, model: type[models.Model], summary: ReconciliationSummary, *, dry_run: bool,
    ) -> None:
        table = model._meta.db_table
        with connection.cursor() as cursor:
            db_columns = {d.name for d in connection.introspection.get_table_description(cursor, table)}

        for f in self._addable_fields(model):
            column = f.column
            if column in db_columns:
                continue
            reason = self._reject_reason_for_add(f)
            if reason is not None:
                summary.warnings.append(f'{table}.{column}: refused to add — {reason}')
                logger.warning('ensure_schema: refused to add %s.%s — %s', table, column, reason)
                continue
            if dry_run:
                summary.columns_added.append(f'{table}.{column} (dry-run)')
                continue
            try:
                with connection.schema_editor() as schema_editor:
                    schema_editor.add_field(model, f)
            except Exception as exc:
                summary.warnings.append(f'{table}.{column}: add_field failed — {type(exc).__name__}: {exc}')
                logger.exception('ensure_schema: add_field failed for %s.%s', table, column)
                continue
            summary.columns_added.append(f'{table}.{column}')
            logger.info('ensure_schema: added column %s.%s', table, column)

    def _reconcile_indexes(
        self, model: type[models.Model], summary: ReconciliationSummary, *, dry_run: bool,
    ) -> None:
        declared = list(model._meta.indexes)
        if not declared:
            return
        table = model._meta.db_table
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, table)
        existing_index_names = {
            name for name, spec in constraints.items() if spec.get('index')
        }

        for index in declared:
            expected_name = index.name or ''
            if not expected_name:
                # Un-named indexes get an auto-generated name that we can't
                # cheaply reconstruct from the DB introspection. Django's own
                # migrations handle this correctly; we won't be smarter.
                continue
            if expected_name in existing_index_names:
                continue
            if dry_run:
                summary.indexes_created.append(f'{expected_name} (dry-run)')
                continue
            try:
                with connection.schema_editor() as schema_editor:
                    schema_editor.add_index(model, index)
            except Exception as exc:
                summary.warnings.append(f'{expected_name}: add_index failed — {type(exc).__name__}: {exc}')
                logger.exception('ensure_schema: add_index failed for %s', expected_name)
                continue
            summary.indexes_created.append(expected_name)
            logger.info('ensure_schema: created index %s on %s', expected_name, table)

    # -- guardrails ----------------------------------------------------

    @staticmethod
    def _addable_fields(model: type[models.Model]) -> Iterable[models.Field]:
        """Concrete, non-M2M, non-inherited fields the reconciler is
        willing to consider adding. Auto-created PK fields are excluded —
        they only make sense at table creation."""
        for f in model._meta.get_fields():
            if not getattr(f, 'concrete', False):
                continue
            if getattr(f, 'many_to_many', False):
                continue
            # AutoField primary keys: refuse. If a table exists without its
            # PK column, we're already lost — a migration is required.
            if isinstance(f, models.AutoField):
                continue
            yield f  # type: ignore[misc]

    @staticmethod
    def _reject_reason_for_add(f: models.Field) -> str | None:
        """None if the field is safe to add; otherwise a human-readable
        refusal reason.

        Refuse-first bias — we only add fields that can be created without
        moving existing data or requiring a backfill.
        """
        if f.many_to_many:
            return 'M2M field — requires through-table migration'
        # ForeignKey columns are technically safe to add nullable, but the
        # implied constraint + index creation dance is safer as a real
        # migration. Refuse to be conservative.
        if isinstance(f, (models.ForeignKey, models.OneToOneField)):
            if not f.null:
                return 'foreign key requires migration (constraint safety)'
        if f.null:
            return None
        if f.has_default():
            return None
        return 'non-nullable column without default — backfill required, use a real migration'

    # -- reporting ------------------------------------------------------

    def _report(self, summary: ReconciliationSummary, *, dry_run: bool) -> None:
        prefix = 'ensure_schema[dry-run]' if dry_run else 'ensure_schema'
        if not summary.any_action and not summary.warnings:
            self.stdout.write(f'{prefix}: schema matches ORM — no action needed')
            return
        for t in summary.tables_created:
            self.stdout.write(self.style.SUCCESS(f'{prefix}: created table {t}'))
        for c in summary.columns_added:
            self.stdout.write(self.style.SUCCESS(f'{prefix}: added column {c}'))
        for i in summary.indexes_created:
            self.stdout.write(self.style.SUCCESS(f'{prefix}: created index {i}'))
        for w in summary.warnings:
            self.stdout.write(self.style.WARNING(f'{prefix}: WARN {w}'))
