"""Guards against SQL this app's tests cannot catch by running.

The suite runs on SQLite; production is Postgres. Most differences surface as
a failed query somewhere, but a few are accepted silently by SQLite and are a
hard syntax error on Postgres — those reach production untested, because every
local run is green.

``test_migrations.py`` covers the same hazard for DDL. This covers queries.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES = sorted(p for p in ROOT.glob("*.py")) + \
          sorted(ROOT.glob("services/*.py"))

# The only operands Postgres allows on the right of IS / IS NOT.
LEGAL = {"None", "True", "False"}


def _is_comparison_calls(path):
    """Yield (line, method, argument-source) for every .is_()/.isnot() call."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("is_", "isnot", "is_not"):
            continue
        if len(node.args) != 1:
            continue
        yield node.lineno, func.attr, ast.unparse(node.args[0])


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_is_comparisons_use_operands_postgres_allows(path):
    """``col.isnot(5)`` compiles to ``col IS NOT 5``, which Postgres rejects.

    SQLite generalises IS/IS NOT to any operand, so a query written this way
    passes every test here and then 500s in production with

        ERROR: syntax error at or near "5"

    That is exactly how the nearby-sightings page shipped broken: the filter
    read ``Sighting.pet_id.isnot(pet.id)``. Use ``!=`` instead, and add an
    explicit ``.is_(None)`` arm if null rows should also match — ``!=`` is
    UNKNOWN rather than true against NULL, which is the reason the wrong
    spelling looked appealing in the first place.
    """
    offenders = [
        f"{path.name}:{line} .{method}({arg})"
        for line, method, arg in _is_comparison_calls(path)
        if arg not in LEGAL
    ]
    assert not offenders, (
        "IS / IS NOT with an operand Postgres does not accept:\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_would_catch_the_bug_it_was_written_for(tmp_path):
    """A guard nobody has seen fail is a guard nobody should trust."""
    bad = tmp_path / "bad.py"
    bad.write_text("q.filter(Sighting.pet_id.isnot(pet.id))\n", encoding="utf-8")
    found = [arg for _, _, arg in _is_comparison_calls(bad) if arg not in LEGAL]
    assert found == ["pet.id"]


def test_the_guard_allows_the_legitimate_spellings(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text(
        "q.filter(Pet.is_removed.is_(False))\n"
        "q.filter(Sighting.pet_id.is_(None))\n"
        "q.filter(SearchTrack.finished_at.isnot(None))\n",
        encoding="utf-8")
    assert [arg for _, _, arg in _is_comparison_calls(ok) if arg not in LEGAL] == []
