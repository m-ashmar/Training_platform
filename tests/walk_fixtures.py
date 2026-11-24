"""The ONE fixture module every walk-state test uses (`manage.py seed_walk_states`,
`users/walk/`). Import the fixtures into your area's test file::

    from tests.walk_fixtures import walk_platform, walk_run  # noqa: F401

    def test_client_waiting_for_coach(walk_run, api):
        entry = walk_run("client_waiting_for_coach")      # its manifest entry
        ...                                                 # read it through the API

**`walk_platform`** gives THIS test's database what a dev server has after
`manage.py bootstrap_platform`, by running that command itself, so it can never drift:

* the channel capabilities, the quota features, the subscription plans (a paid plan on
  sale), the diet plan templates, the badges (achievements and social achievements), the
  starter exercises and the platform's templates (the free first workouts and the
  starter templates) — `bootstrap_platform --skip-food-catalogue`, inside the test's own
  transaction, so every row is rolled back with the test (about a second);
* the food catalogue (foods, recipes, household units) — the session's
  `seeded_catalogue`, built once per session and re-seeded after a truncation, so a
  diet plan can be generated;
* `MEDIA_ROOT` and `PRIVATE_MEDIA_ROOT` in the test's own `tmp_path`, so an upload a state
  makes through the real route (a trainer's verification documents, a picture) never
  lands in the project's real media folders;
* `WALK_ACCOUNTS_PASSWORD` set for the command.

It answers the password, for a test that signs in with it.

**`walk_run(name, ...)`** resets states through the command itself
(`seed_walk_states --only <name> --manifest <tmp>`), with the after-commit work executed
as it is outside a test (counts, notices, recounts), and answers the state's manifest
entry (or, for several names, the whole manifest). The manifest stays in `tmp_path`.
"""
import io
import json

import pytest

WALK_PASSWORD = "Walk-Throughs-2026!"


@pytest.fixture
def walk_platform(db, seeded_catalogue, settings, tmp_path, monkeypatch):
    from django.core.management import call_command

    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.PRIVATE_MEDIA_ROOT = str(tmp_path / "private_media")
    monkeypatch.setenv("WALK_ACCOUNTS_PASSWORD", WALK_PASSWORD)
    call_command("bootstrap_platform", "--skip-food-catalogue", stdout=io.StringIO())
    return WALK_PASSWORD


@pytest.fixture
def walk_run(walk_platform, tmp_path, django_capture_on_commit_callbacks):
    from django.core.management import call_command

    manifest = tmp_path / "walk_states.json"

    def run(*names, extra=()):
        args = []
        for name in names:
            args += ["--only", name]
        with django_capture_on_commit_callbacks(execute=True):
            call_command("seed_walk_states", *args, "--manifest", str(manifest), *extra,
                         stdout=io.StringIO())
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return data["states"][names[0]] if len(names) == 1 else data

    run.manifest = manifest
    return run
