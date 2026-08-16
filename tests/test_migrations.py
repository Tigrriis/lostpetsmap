"""The migrations must produce DDL Postgres will actually accept.

This exists because of a real deploy failure. `flask db migrate` was run against
the local SQLite database, so Alembic rendered the models' `db.false()` into the
literal `sa.text('0')` — correct for SQLite, and rejected outright by Postgres:

    column "is_banned" is of type boolean but default expression is of type integer

Every test passed, because every test ran on SQLite. The only thing that caught
it was production. So the check belongs here: render the migrations offline
against the Postgres dialect (no server needed) and assert the DDL is sane.
"""
import os
import re
import subprocess
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A dialect-aware server_default renders as the keyword; a SQLite-flavoured one
# renders as an integer literal, which is the bug.
BAD_BOOLEAN_DEFAULT = re.compile(r"\bBOOLEAN\s+DEFAULT\s+[01]\b", re.IGNORECASE)


def _render_offline(database_url: str) -> str:
    """Return the DDL Alembic would emit for a fresh database on that dialect.

    `--sql` is Alembic's offline mode: it prints the statements instead of
    running them, so this needs no live server of either kind.
    """
    env = {**os.environ, "DATABASE_URL": database_url, "FLASK_APP": "app.py"}
    result = subprocess.run(
        [sys.executable, "-m", "flask", "db", "upgrade", "--sql"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, (
        f"offline render failed for {database_url}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return result.stdout


@pytest.fixture(scope="module")
def postgres_ddl():
    # Credentials are never used — offline mode does not connect.
    return _render_offline("postgresql://user:pass@localhost:5432/petmap")


def test_no_integer_defaults_on_boolean_columns(postgres_ddl):
    offenders = BAD_BOOLEAN_DEFAULT.findall(postgres_ddl)
    assert not offenders, (
        f"{len(offenders)} boolean column(s) carry an integer default: {offenders}. "
        "Postgres rejects these. Use sa.false() / sa.true() in the migration, not "
        "sa.text('0') / sa.text('1') — see the note in the baseline migration.")


def test_boolean_defaults_render_as_keywords(postgres_ddl):
    """Positive check, so the test can't pass by rendering no booleans at all."""
    assert re.search(r"\bBOOLEAN DEFAULT false\b", postgres_ddl)
    assert re.search(r"\bBOOLEAN DEFAULT true\b", postgres_ddl)


def test_every_model_table_is_created(postgres_ddl):
    """A table the models declare but no migration builds fails only on deploy."""
    from extensions import db
    import models  # noqa: F401  (registers the tables on the metadata)

    for table in db.metadata.tables:
        assert re.search(rf"CREATE TABLE {re.escape(table)}\b", postgres_ddl), \
            f"no migration creates {table!r}"
