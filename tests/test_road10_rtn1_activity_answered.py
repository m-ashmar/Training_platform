"""The fix run's RTN-1, G9 (2026-11-24): the trainer can tell whether the client answered
the activity level. It defaults to `Sedentary`, so "never asked" and "sedentary" read
the same on the client's file, and `tdee` was computed from that default. Now:

  GET /api/auth/trainer/client-profile/{id}/ (and the list)   `activity_level_answered`;
                                                               `tdee` 0.0 while false
  GET /api/auth/trainer/clients/                              `activity_level_answered`

Both additive; null with the rest of the record while the coaching is paused (B-201).
The unanswered tests fail on the code before; the others hold what stays.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_coaching_freeze import _coached, _freeze

pytestmark = pytest.mark.django_db

PROFILE = "/api/auth/trainer/client-profile/"
ROSTER = "/api/auth/trainer/clients/"


@pytest.fixture(autouse=True)
def coaching_rules(settings):
    settings.COACHING_RENEWAL_REMINDER_DAYS = 3
    settings.COACHING_FROZEN_REMINDER_DAYS = (3, 7, 14, 30)


@pytest.fixture(autouse=True)
def a_cold_cache():
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


def _pair(make_user, api, tag, **record):
    trainer, client, relation, *_rest = _coached(make_user, api, tag)
    type(client).objects.filter(pk=client.pk).update(
        weight=80, height=180, age=30, gender="Male", **record)
    return trainer, client, relation


def _file(api, trainer, client):
    resp = api(trainer).get(f"{PROFILE}{client.pk}/")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _roster_row(api, trainer, client):
    rows = api(trainer).get(ROSTER).json()["clients"]
    return next(row for row in rows if row["id"] == client.pk)


def test_an_unanswered_level_says_so_and_gives_no_tdee(make_user, api):
    trainer, client, _relation = _pair(make_user, api, "g9none",
                                       activity_level="Sedentary",
                                       activity_level_answered_at=None)
    row = _file(api, trainer, client)
    assert (row["activity_level"], row["activity_level_answered"]) == ("Sedentary", False)
    assert row["tdee"] == 0.0 and row["bmr"] > 0
    assert _roster_row(api, trainer, client)["activity_level_answered"] is False


def test_a_chosen_sedentary_level_is_answered(make_user, api):
    trainer, client, _relation = _pair(make_user, api, "g9sed",
                                       activity_level="Sedentary",
                                       activity_level_answered_at=timezone.now())
    row = _file(api, trainer, client)
    assert row["activity_level_answered"] is True and row["tdee"] > 0
    assert _roster_row(api, trainer, client)["activity_level_answered"] is True


def test_a_level_from_before_the_stamp_counts_when_it_is_not_the_default(make_user, api):
    trainer, client, _relation = _pair(make_user, api, "g9old",
                                       activity_level="Moderate",
                                       activity_level_answered_at=None)
    row = _file(api, trainer, client)
    assert row["activity_level_answered"] is True and row["tdee"] > 0


def test_a_paused_coaching_holds_the_flag_back_with_the_record(make_user, api):
    trainer, client, relation = _pair(make_user, api, "g9frz",
                                      activity_level="Moderate",
                                      activity_level_answered_at=timezone.now()
                                      - timedelta(days=3))
    _freeze(relation)
    row = _file(api, trainer, client)
    assert row["coaching_refusal"] == "coaching_frozen"
    assert row["activity_level_answered"] is None and row["tdee"] is None
    assert _roster_row(api, trainer, client)["activity_level_answered"] is None
