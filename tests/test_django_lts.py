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

These tests read files and installed-package metadata only: no database.
"""
import importlib.metadata
import re
from pathlib import Path

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
