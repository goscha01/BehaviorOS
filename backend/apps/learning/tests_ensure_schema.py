"""Tests for the additive-only schema reconciler (`ensure_schema`).

Coverage matrix from the spec:
  1. table exists / column missing            → column added, second run no-op
  2. table missing                             → table created
  3. table + columns already present           → no-op
  4. nullable field addition                   → succeeds
  5. index creation                            → succeeds when named + missing
  6. unsupported/destructive drift             → reported (warning), NOT modified
  7. non-nullable field without default        → refused

Uses TransactionTestCase because DDL operations (DROP COLUMN / CREATE TABLE)
can't be rolled back inside the wrapping transaction that Django's TestCase
uses, and the reconciler's schema_editor calls must be visible to the same
connection that runs the assertions.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase


def _columns(table: str) -> set[str]:
    with connection.cursor() as cursor:
        return {d.name for d in connection.introspection.get_table_description(cursor, table)}


def _indexes(table: str) -> set[str]:
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, table)
    return {name for name, spec in constraints.items() if spec.get('index')}


def _drop_column(table: str, column: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE {table} DROP COLUMN {column}')


def _drop_index(name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f'DROP INDEX IF EXISTS {name}')


def _drop_table(table: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f'DROP TABLE IF EXISTS {table} CASCADE')


class EnsureSchemaColumnAddTest(TransactionTestCase):
    """Missing column on an existing table → reconciler adds it."""

    def test_missing_column_is_added_then_second_run_is_noop(self):
        table = 'context_evidenceevent'
        self.assertIn('promoted_at', _columns(table),
                      msg='fixture: promoted_at should exist before we drop it')

        _drop_column(table, 'promoted_at')
        self.assertNotIn('promoted_at', _columns(table))

        out = StringIO()
        call_command('ensure_schema', '--apps', 'context', stdout=out)
        self.assertIn('added column context_evidenceevent.promoted_at', out.getvalue())
        self.assertIn('promoted_at', _columns(table))

        out = StringIO()
        call_command('ensure_schema', '--apps', 'context', stdout=out)
        self.assertIn('no action needed', out.getvalue())


class EnsureSchemaTableCreateTest(TransactionTestCase):
    """Missing table → reconciler creates it (existing behavior preserved)."""

    def test_missing_learning_table_gets_created(self):
        # RejectedSuggestionSignature is a small model in the learning app
        # that's safe to drop and recreate for the test.
        from apps.learning.models import RejectedSuggestionSignature
        table = RejectedSuggestionSignature._meta.db_table

        _drop_table(table)
        with connection.cursor() as cursor:
            existing = set(connection.introspection.table_names(cursor))
        self.assertNotIn(table, existing)

        out = StringIO()
        call_command('ensure_schema', '--apps', 'learning', stdout=out)
        self.assertIn(f'created table {table}', out.getvalue())
        with connection.cursor() as cursor:
            existing = set(connection.introspection.table_names(cursor))
        self.assertIn(table, existing)


class EnsureSchemaNoopTest(TransactionTestCase):
    """Fresh test DB → nothing to reconcile → clean report."""

    def test_matching_schema_is_a_noop(self):
        out = StringIO()
        call_command('ensure_schema', stdout=out)
        self.assertIn('no action needed', out.getvalue())


class EnsureSchemaIndexAddTest(TransactionTestCase):
    """Named index in Meta.indexes missing from DB → reconciler creates it."""

    def test_missing_named_index_is_created(self):
        table = 'context_evidenceevent'
        index_name = 'ctx_event_promo_queue_idx'

        _drop_index(index_name)
        self.assertNotIn(index_name, _indexes(table))

        out = StringIO()
        call_command('ensure_schema', '--apps', 'context', stdout=out)
        self.assertIn(f'created index {index_name}', out.getvalue())
        self.assertIn(index_name, _indexes(table))


class EnsureSchemaRefusalTest(TransactionTestCase):
    """A non-nullable-no-default field CANNOT be added by the reconciler.

    We simulate the drift by dropping a column that has neither null nor
    default, then verifying the reconciler REPORTS the drift as a warning
    rather than mutating the schema. `runtime` on EvidenceEvent is a
    CharField without null/default — perfect regression target.
    """

    def test_non_nullable_without_default_is_refused(self):
        table = 'context_evidenceevent'
        column = 'runtime'
        self.assertIn(column, _columns(table))

        _drop_column(table, column)
        self.assertNotIn(column, _columns(table))

        out = StringIO()
        err = StringIO()
        call_command('ensure_schema', '--apps', 'context', stdout=out, stderr=err)

        combined = out.getvalue() + err.getvalue()
        self.assertIn('refused to add', combined)
        self.assertIn(f'{table}.{column}', combined)
        self.assertNotIn(column, _columns(table),
                         msg='reconciler must NOT auto-add a non-nullable-no-default column')

    def test_strict_mode_exits_nonzero_on_warnings(self):
        _drop_column('context_evidenceevent', 'runtime')

        with self.assertRaises(SystemExit) as ctx:
            call_command('ensure_schema', '--apps', 'context', '--strict', stdout=StringIO())
        self.assertEqual(ctx.exception.code, 2)


class EnsureSchemaDryRunTest(TransactionTestCase):
    """--dry-run reports drift without touching the DB."""

    def test_dry_run_does_not_add_column(self):
        table = 'context_evidenceevent'
        _drop_column(table, 'promoted_at')
        self.assertNotIn('promoted_at', _columns(table))

        out = StringIO()
        call_command('ensure_schema', '--apps', 'context', '--dry-run', stdout=out)
        self.assertIn('(dry-run)', out.getvalue())
        self.assertNotIn('promoted_at', _columns(table))
