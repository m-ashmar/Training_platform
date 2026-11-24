"""The fix run's SOC-2, S-190 (2026-11-24): no method stacks the same decorator twice.
`achievements/engine.py` `_flush_progress` was `@classmethod @classmethod`, and so was
`_award_achievement` (a stray one, two blank lines above its own): chaining
classmethod descriptors works on the pinned Python 3.12 only (deprecated in 3.11,
removed in 3.13), where the progress write and the awards themselves would fail. Held in
the source, for every first-party module. Fails on the code before.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".venv", "_excluded", "yalla_gym", "mobile", "flutter", "node_modules", "scripts",
        "migrations", "staticfiles", "media", ".git", ".claude"}


def _first_party():
    for path in ROOT.rglob("*.py"):
        if not SKIP.intersection(path.relative_to(ROOT).parts):
            yield path


def test_no_decorator_is_stacked_twice():
    doubled = []
    for path in _first_party():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = [ast.unparse(d) for d in node.decorator_list]
            for first, second in zip(names, names[1:]):
                if first == second and first in ("classmethod", "staticmethod",
                                                 "property"):
                    doubled.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.name}")
    assert doubled == [], doubled
