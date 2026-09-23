"""BACKEND_BUGS B-037..B-046: what the app session found wiring home and chat
(2026-09-23), one test per row the backend had not already closed.

B-039, B-040 and B-041 were fixed by the side-bug run before these rows reached the
backend (SIDE_BUGS S-105 and S-063); their tests live with those fixes.
"""
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from tests.test_home import _challenge, _data, _home, _log_sets, _message, _program

pytestmark = pytest.mark.django_db

JSON = "application/json"
REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------------ B-037
def test_weight_lifted_counts_when_the_coach_prescribed_none(make_user, api):
    """The common routine is sets and reps with the weight left to the client. Its logged
    volume sat inside the "weight was prescribed" branch, so every such day recorded
    0.0 kg while analytics/strength counted the same sets."""
    from routine.outcomes import day_counts

    trainer, client, routine, rows = _program(make_user, "b037", sets=2, reps=10,
                                              weight=None)
    _log_sets(client, api, rows[0], 2, reps=10, weight=40)

    counts = day_counts(routine, client, 1)
    assert counts['logged_volume_kg'] == 800.0, "2 sets x 10 reps x 40 kg"
    assert counts['planned_volume_kg'] is None, "no weight was planned, so no plan to meet"


# ------------------------------------------------------------------------ B-038
def test_the_asgi_module_imports_cold_as_daphne_imports_it():
    """Daphne imports `training_platform.asgi` with NO prior `django.setup()`. The
    routing modules were imported before `get_asgi_application()`, and `chat.routing`
    reaches a model at import time, so production could not start."""
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="training_platform.settings_local")
    run = subprocess.run(
        [sys.executable, "-c", "import training_platform.asgi as a; "
                               "print(type(a.application).__name__)"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    assert "ProtocolTypeRouter" in run.stdout
    assert "AppRegistryNotReady" not in run.stderr


# ------------------------------------------------------------------------ B-042
def test_every_chat_message_says_what_kind_it_is(make_user, api):
    """The app switches on `kind` before any second kind exists."""
    trainer, client, routine, rows = _program(make_user, "b042")
    message = _message(trainer, client, body="hello")

    from chat.serializers import MessageSerializer
    assert MessageSerializer(message).data["kind"] == "text"


# ------------------------------------------------------------------------ B-043
def test_a_cached_home_reports_the_time_left_not_a_fresh_120(make_user, api):
    """A page served from a cache entry already two minutes old told the app to wait
    another 120 seconds."""
    from unittest import mock

    from home import views as home_views

    trainer, client, routine, rows = _program(make_user, "b043")
    first = _home(api, client)
    assert first["refresh_after_seconds"] == home_views.CACHE_SECONDS

    later = timezone.now() + timedelta(seconds=100)
    with mock.patch("home.views.timezone.now", return_value=later):
        second = _home(api, client)
    assert second["as_of"] == first["as_of"], "served from the cache"
    assert 1 <= second["refresh_after_seconds"] <= 20


# ------------------------------------------------------------------------ B-044
def test_the_contract_says_the_minutes_can_be_null():
    from home.views import HomeView

    doc = HomeView.get.__doc__ or HomeView.__doc__
    assert "NULL when the coach set none" in (HomeView.__doc__ or "") + (doc or "")


# ------------------------------------------------------------------------ B-045
def test_days_before_the_program_starts_are_not_planned(make_user, api):
    """A program starting mid-week marked the days before it as planned training."""
    from routine.models import Routine

    trainer, client, routine, rows = _program(make_user, "b045", days=1)
    today = timezone.localdate()
    # A program that runs for today only: every other day of the strip is before it
    # starts or after it ends, on whichever weekday this runs (the strip begins on
    # Saturday, so "the days before today" alone is empty one day in seven).
    Routine.objects.filter(pk=routine.pk).update(start_date=today, end_date=today,
                                                 is_active=True)

    hero = _data(_home(api, client), "workout_hero")
    outside = [d for d in hero["week_strip"] if d["date"] != today.isoformat()]
    assert len(outside) == 6
    assert {d["state"] for d in outside} <= {"rest", "done"}, outside


# ------------------------------------------------------------------------ B-046
def test_participants_are_counted_by_the_rule_that_ranks_them(make_user, api):
    """`participants` read the denormalised column while `rank` was counted live."""
    from social.models import Challenge

    trainer, client, routine, rows = _program(make_user, "b046")
    challenge = _challenge(client, "b046", others=2)
    Challenge.objects.filter(pk=challenge.pk).update(participants_count=58)

    rank = _data(_home(api, client), "challenge_rank")
    assert rank["participants"] == 3, "the three people actually in the standing"
    assert rank["rank"] <= rank["participants"]
