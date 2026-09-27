"""The framework on a supported line, and the image on exactly what the gate ran on.

Two findings of the production-readiness audit (2026-09-26), fixed together because the
second is what keeps the first fixed:

* quality lens — `Django==5.1.3` was pinned. The 5.1 series stopped receiving security
  fixes in December 2025, and 5.1.3 had already missed every 5.1.x security release after
  it. `Pillow==11.0.0` decodes every image a user uploads
  (`training_platform/file_security.py`). Django is now on the 5.2 LTS line (security
  fixes until April 2028) and Pillow on the newest release.
* deploy lens — dependencies were unpinned. `requirements.txt` carried `daphne>=4.0.0`,
  `openai>=1.0.0`, `firebase-admin>=6.0.0`, a bare `django-modeltranslation` and a bare
  `matplotlib`, nothing pinned the ~130 transitive packages, and the Dockerfile ran a
  second, unpinned `pip install daphne`. Every `fly deploy` resolved whatever was newest
  that day, so the image could run versions no test had ever run on.

`requirements.txt` stays the list a person edits (one exact pin per direct dependency,
with the reason it is there). `requirements.lock` is generated from it and pins every
package, transitive ones included, with hashes; the Dockerfile installs only the lock.
How to refresh it is written at the top of `requirements.txt`.

The last section holds the upgrade's own debts: the project starts with no deprecation
warning and no system-check issue, and nothing calls an API Django 6.0 removes.

These tests read files, installed-package metadata and a `manage.py check` subprocess
only: no database.
"""
import ast
import importlib.metadata
import os
import re
import subprocess
import sys
import warnings
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
HUMAN = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"
DOCKERFILE = ROOT / "Dockerfile"

#: The newest 5.2.x on the day of the upgrade. A later 5.2 patch passes; 5.1, or a jump
#: to 6.x (which needs its own migration work), does not.
DJANGO_FLOOR = (5, 2, 17)
#: The newest Pillow on the day of the upgrade. Pillow parses untrusted uploads, so it
#: is the one package whose floor this file names besides the framework itself.
PILLOW_FLOOR = Version("12.3.0")

REFRESH_HINT = (
    "Install exactly what the image installs:\n"
    "    .venv/bin/pip install --require-hashes -r requirements.lock\n"
    "and refresh the lock from requirements.txt as its header says.")


def _logical_lines(path):
    """The requirement lines of a pip requirements file: continuations joined, comments
    and blank lines dropped."""
    text = path.read_text().replace("\\\n", " ")
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        yield line


def _parse(line):
    """(Requirement, [hashes]) of one logical line; pip options other than --hash are
    rejected, because the lock is meant to be installed with nothing else."""
    parts = line.split()
    hashes = [p.split("=", 1)[1] for p in parts if p.startswith("--hash=")]
    rest = " ".join(p for p in parts if not p.startswith("--hash="))
    assert not rest.startswith("-"), f"unexpected pip option in {line!r}"
    return Requirement(rest), hashes


def _lock():
    pins = {}
    for line in _logical_lines(LOCK):
        req, hashes = _parse(line)
        pins.setdefault(canonicalize_name(req.name), []).append((req, hashes))
    return pins


def _exact_version(req):
    specs = list(req.specifier)
    if len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
        return None
    return specs[0].version


def _applies(req):
    return req.marker is None or req.marker.evaluate()


# ---------------------------------------------------------------------------
# 1. The versions themselves
# ---------------------------------------------------------------------------

def test_django_is_on_the_5_2_lts_line():
    import django

    assert django.VERSION[:2] == (5, 2), (
        f"Django {django.get_version()} is not on the 5.2 LTS line; 5.1 is out of "
        f"security support and 6.x is a separate migration")
    assert django.VERSION[:3] >= DJANGO_FLOOR, (
        f"Django {django.get_version()} is older than {DJANGO_FLOOR}, which already "
        f"shipped security fixes")


def test_pillow_is_at_least_the_release_the_upgrade_chose():
    import PIL

    assert Version(PIL.__version__) >= PILLOW_FLOOR, (
        f"Pillow {PIL.__version__} decodes every uploaded image; the floor is "
        f"{PILLOW_FLOOR}")


# ---------------------------------------------------------------------------
# 2. The pins
# ---------------------------------------------------------------------------

def test_the_lock_pins_every_package_exactly_and_by_hash():
    """A range, a bare name or a missing hash anywhere in the lock is what the audit
    found: an install that resolves on the day it runs, not on the day it was tested."""
    assert LOCK.exists(), "requirements.lock is missing: the image has nothing to install"
    pins = _lock()
    assert len(pins) > 40, "the lock pins far fewer packages than the app imports"

    loose, unhashed = [], []
    for name, entries in pins.items():
        for req, hashes in entries:
            if _exact_version(req) is None:
                loose.append(str(req))
            if not hashes:
                unhashed.append(name)
    assert not loose, f"not pinned to one exact version: {loose}"
    assert not unhashed, f"no hash, so pip --require-hashes refuses the lock: {unhashed}"


def test_every_direct_dependency_is_pinned_and_agrees_with_the_lock():
    """requirements.txt is the list a person edits, and the lock is generated from it.
    A direct dependency without an exact pin, or with a pin the lock does not carry,
    means the lock was not regenerated after the edit."""
    pins = _lock()
    unpinned, missing, disagree = [], [], []
    for line in _logical_lines(HUMAN):
        req, _ = _parse(line)
        version = _exact_version(req)
        if version is None:
            unpinned.append(str(req))
            continue
        locked = [r for r, _ in pins.get(canonicalize_name(req.name), [])]
        if not locked:
            missing.append(req.name)
        elif all(_exact_version(r) != version for r in locked):
            disagree.append(f"{req.name}: requirements.txt {version}, lock "
                            f"{sorted({_exact_version(r) for r in locked})}")
    assert not unpinned, f"direct dependencies without an exact pin: {unpinned}"
    assert not missing, f"direct dependencies absent from requirements.lock: {missing}"
    assert not disagree, "requirements.lock is stale:\n" + "\n".join(disagree)


def test_the_image_installs_the_lock_and_nothing_else():
    """The Dockerfile's pip must read the lock in hash-checking mode, and nothing may be
    installed beside it: the old second `pip install daphne` pulled the newest daphne
    (and its Twisted) into every build."""
    dockerfile = DOCKERFILE.read_text().replace("\\\n", " ")
    installs = [line for line in dockerfile.splitlines()
                if re.search(r"\bpip3?\s+install\b", line) and not line.lstrip().startswith("#")]
    assert len(installs) == 1, f"expected ONE pip install in the Dockerfile, found {installs}"
    (install,) = installs
    assert "-r requirements.lock" in install, install
    assert "--require-hashes" in install, install
    assert re.search(r"^COPY\s+requirements\.lock\b", dockerfile, re.M), \
        "the lock is not copied into the image before the install"
    assert not re.search(r"-r\s+requirements\.txt", dockerfile), \
        "the image installs the unpinned human list"


def test_the_gate_runs_on_the_versions_the_image_ships():
    """The point of the lock: the versions the suite passes on are the versions that
    deploy. Every package the lock pins for this platform must be installed at exactly
    that version here; a stale virtualenv fails this test instead of quietly proving the
    old versions work."""
    wrong, absent = [], []
    for name, entries in _lock().items():
        applicable = [r for r, _ in entries if _applies(r)]
        if not applicable:
            continue
        wanted = {_exact_version(r) for r in applicable}
        try:
            have = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            absent.append(name)
            continue
        if Version(have) not in {Version(v) for v in wanted}:
            wrong.append(f"{name}: installed {have}, lock {sorted(wanted)}")
    assert not absent and not wrong, (
        "this environment is not the image's:\n"
        + "\n".join(wrong + [f"{n}: not installed" for n in absent])
        + "\n" + REFRESH_HINT)


@pytest.mark.parametrize("name", ["daphne", "openai", "firebase-admin",
                                  "django-modeltranslation", "matplotlib"])
def test_the_packages_the_audit_named_are_pinned(name):
    """The five entries the deploy audit quoted, each by name, so the finding cannot
    come back one package at a time."""
    reqs = [_parse(line)[0] for line in _logical_lines(HUMAN)]
    (req,) = [r for r in reqs if canonicalize_name(r.name) == canonicalize_name(name)]
    assert _exact_version(req), f"{name} is not pinned in requirements.txt: {req}"


# ---------------------------------------------------------------------------
# 3. What the upgrade deprecated, and what Django 6.0 removes
# ---------------------------------------------------------------------------

#: Directories that are not this project's source: vendored environments, the
#: abandoned clients, other worktrees under .claude/, and the tests themselves.
_NOT_SOURCE = {".venv", "venv", "_excluded", "mobile", "flutter", "yalla_gym", "tests",
               "node_modules", ".claude", ".git", "staticfiles", "media", "__pycache__"}


def _project_sources():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _NOT_SOURCE]
        for name in filenames:
            if name.endswith(".py"):
                path = Path(dirpath) / name
                yield path.relative_to(ROOT), path


def test_the_project_starts_without_a_deprecation_warning_or_a_check_issue():
    """Deprecations are how the NEXT upgrade breaks. On b204be6 the start-up printed
    RemovedInDjango60Warning (`CheckConstraint(check=...)` in diet/models.py) and
    drf-yasg's compat-renderer DeprecationWarning; after the 5.2 upgrade, allauth 65
    added three system-check warnings for the settings it renamed. A subprocess, because
    the warnings fire once, at import, and pytest.ini ignores DeprecationWarning."""
    result = subprocess.run(
        [sys.executable, "-W", "always::DeprecationWarning",
         "-W", "always::PendingDeprecationWarning",
         "manage.py", "check", "--settings=training_platform.settings_local"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-3000:]
    assert "System check identified no issues" in output, output[-3000:]
    deprecations = [line for line in output.splitlines()
                    if re.search(r"(Deprecation|RemovedInDjango\d+)Warning", line)]
    assert not deprecations, "\n".join(deprecations)


def test_nothing_calls_what_django_6_removes():
    """Two calls that warn on 5.2 and fail on 6.0, found by this upgrade:
    `format_html()` with no arguments (a TypeError in 6.0; seven admin badges called
    it) and `CheckConstraint(check=...)` (the keyword is `condition`). The admin ones
    warn only when a page renders, so start-up alone would never show them."""
    offenders = []
    for rel, path in _project_sources():
        tree = ast.parse(path.read_text(), filename=str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            starred = any(isinstance(a, ast.Starred) for a in node.args)
            if (name == "format_html" and len(node.args) <= 1 and not node.keywords
                    and not starred):
                offenders.append(f"{rel}:{node.lineno} format_html() without arguments")
            if name == "CheckConstraint" and any(k.arg == "check" for k in node.keywords):
                offenders.append(f"{rel}:{node.lineno} CheckConstraint(check=...)")
    assert not offenders, "\n".join(offenders)


class _MealRows:
    """Stands in for `plan.meals.values(...).annotate(...)`."""

    def __init__(self, rows):
        self.rows = rows

    def values(self, *fields):
        return self

    def annotate(self, **aggregates):
        return self.rows


def test_the_admin_badges_render_on_5_2_without_the_6_0_warning():
    """Each badge the scan above rewrote, rendered with Django's own 6.0 warning turned
    into an error: same markup as before, text passed as an argument."""
    from django.contrib import admin
    from django.utils import timezone
    from django.utils.deprecation import RemovedInDjango60Warning

    from django.contrib.auth import get_user_model

    from diet.admin import ImprovedDietPlanAdmin, ImprovedFoodItemAdmin, RecipeAdmin
    from diet.models import DietPlan, FoodItem, Recipe
    from users.admin import CustomUserAdmin
    from wallet.admin import ExchangeRateAdmin
    from wallet.currencies import RATE_BASE, RATE_QUOTE
    from wallet.models import ExchangeRate

    foods = ImprovedFoodItemAdmin(FoodItem, admin.site)
    plans = ImprovedDietPlanAdmin(DietPlan, admin.site)
    recipes = RecipeAdmin(Recipe, admin.site)
    users = CustomUserAdmin(get_user_model(), admin.site)
    rates = ExchangeRateAdmin(ExchangeRate, admin.site)
    rice = SimpleNamespace(name="<i>Rice</i>", calories=130, household_unit="", unit_grams=0)
    dish = SimpleNamespace(
        pk=1, nutrition=lambda: {"calories": 195, "protein": 4, "carbs": 42, "fat": 0.4},
        ingredients=SimpleNamespace(all=lambda: [SimpleNamespace(food=rice, grams=150)]))
    today = timezone.localdate()
    later = timezone.now() + timedelta(days=1)
    earlier = timezone.now() - timedelta(days=1)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RemovedInDjango60Warning)
        assert ">needs review</span>" in foods.review_badge(SimpleNamespace(needs_review=True))
        assert ">✓</span>" in foods.review_badge(SimpleNamespace(needs_review=False))
        assert ">none</span>" in foods.slots_display(SimpleNamespace(meal_slots=[]))
        assert ">no unit</span>" in foods.unit_display(
            SimpleNamespace(household_unit=None, unit_grams=None))
        assert plans.status_badge(SimpleNamespace(
            start_date=today - timedelta(days=9), end_date=today - timedelta(days=2))) \
            == '<span style="background:gray;">Completed</span>'
        assert plans.status_badge(SimpleNamespace(
            start_date=today - timedelta(days=1), end_date=today + timedelta(days=1))) \
            == '<span style="background:green;">Active</span>'
        assert plans.status_badge(SimpleNamespace(
            start_date=today + timedelta(days=1), end_date=today + timedelta(days=3))) \
            == '<span style="background:blue;">Upcoming</span>'
        assert ">queued</span>" in rates.status(SimpleNamespace(
            base=RATE_BASE, quote=RATE_QUOTE, fx_live_pk=None, pk=1, effective_from=later))
        assert ">superseded</span>" in rates.status(SimpleNamespace(
            base=RATE_BASE, quote=RATE_QUOTE, fx_live_pk=2, pk=1, effective_from=earlier))
        assert users.password_status(SimpleNamespace(
            password="x", has_usable_password=lambda: True)) \
            == '<span style="color: green;">✓ Set</span>'
        assert users.password_status(SimpleNamespace(
            password="", has_usable_password=lambda: False)) \
            == '<span style="color: red;">✗ Not Set</span>'
        panel = recipes.nutrition_panel(dish)
        breakdown = plans.meal_breakdown(SimpleNamespace(meals=_MealRows([
            {"meal_type": "breakfast", "count": 7},
            {"meal_type": "<b>lunch</b>", "count": 3},
        ])))
    assert ('<tr><td style="padding:2px 12px 2px 0">&lt;i&gt;Rice&lt;/i&gt;</td>'
            '<td style="padding:2px 12px 2px 0">150 g</td>'
            '<td style="padding:2px 12px 2px 0">150 g</td>'
            '<td style="padding:2px 12px 2px 0">195 kcal</td></tr>') in panel, panel
    assert "<i>" not in panel, "a food name is escaped, not pasted into the markup"
    assert breakdown == (
        '<div style="font-family: monospace;"><strong>Meal Distribution:</strong><br>'
        'breakfast: 7<br>&lt;b&gt;lunch&lt;/b&gt;: 3<br></div>'), \
        "a stored value is escaped, not pasted into the markup"
