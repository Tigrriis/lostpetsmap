"""
Bring the database schema up to date.

Two entry points share one implementation:

  * `python bootstrap_db.py` — from the Render start command, and locally.
  * `run_migrations()` — called once from the app on its first request, so the
    schema self-heals on a plain code deploy even when the start command was
    not updated. See app.py.

Unlike the older services this was adapted from, this database has been managed
by Alembic since its first row, so there is no create_all() legacy to adopt.
The stamp branch is kept anyway for the case that matters in practice: someone
running `db.create_all()` by hand in a shell to get a local database going, then
wondering why `flask db upgrade` fails with "table users already exists".

A schema check runs after the upgrade and raises if the tables and columns the
models declare are not all present, so a bad state is a loud failure at boot
rather than a 500 the first time a user signs in.
"""
import os
import sys

from sqlalchemy import inspect

from extensions import db
from flask_migrate import stamp, upgrade

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")


def _baseline_revision() -> str:
    """The single root migration — the one a create_all() database matches.

    Read from the migration scripts rather than hardcoded, but asserted unique:
    a branched history has no single "the schema already exists here" point, so
    stamping would be a guess and we refuse it.
    """
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    cfg = AlembicConfig()
    cfg.set_main_option("script_location", MIGRATIONS_DIR)
    bases = ScriptDirectory.from_config(cfg).get_bases()
    if len(bases) != 1:
        raise RuntimeError(
            f"expected exactly one base migration to stamp, found {bases!r}. "
            "Resolve the branched history before deploying.")
    return bases[0]


def _verify_schema() -> None:
    """Raise if the migrated schema is missing anything the models declare."""
    insp = inspect(db.engine)
    existing = set(insp.get_table_names())
    problems: list[str] = []

    for table_name, table in db.metadata.tables.items():
        if table_name not in existing:
            problems.append(f"missing table {table_name!r}")
            continue
        actual = {c["name"] for c in insp.get_columns(table_name)}
        for column in table.columns:
            if column.name not in actual:
                problems.append(f"{table_name}.{column.name} missing")

    if problems:
        raise RuntimeError(
            "schema does not match the models after upgrade: " + "; ".join(problems)
            + ". The migrations did not fully apply.")


def run_migrations() -> None:
    """Adopt/upgrade the schema. **Requires an active app context.**"""
    tables = set(inspect(db.engine).get_table_names())
    already_tracked = "alembic_version" in tables
    pre_existing = "users" in tables

    if not already_tracked and pre_existing:
        baseline = _baseline_revision()
        print(f"bootstrap_db: existing schema with no migration history - "
              f"stamping the baseline ({baseline}) as applied.")
        stamp(revision=baseline)
    elif not already_tracked:
        print("bootstrap_db: empty database - migrations will build it.")
    else:
        print("bootstrap_db: migration history present.")

    upgrade()
    _verify_schema()
    print("bootstrap_db: schema up to date.")


def main() -> int:
    # Imported here, not at module top, so app.py can import run_migrations
    # without a circular import (app.py imports this module).
    from app import app

    os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance"),
                exist_ok=True)
    with app.app_context():
        run_migrations()
    return 0


if __name__ == "__main__":
    sys.exit(main())
