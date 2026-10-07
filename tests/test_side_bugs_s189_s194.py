"""SIDE_BUGS S-189 and S-194: the challenges list is cached per person for 120 s, and
only the ACTING person's entry was ever dropped.

* S-189 — an operator hiding a challenge left it in every list for up to two minutes,
  while its detail answered 404 at once.
* S-194 — a creator's update, close or delete, and another person's join, left
  everyone else's cached list showing the old title, status and participants_count.

Both now carry a version every reader's list shares (`social/challenge_cache.py`).
"""
from datetime import timedelta

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def a_cold_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _challenge(api, creator, title):
    now = timezone.now()
    made = api(creator).post("/api/social/challenges/", {
        "title": title, "description": "cache", "challenge_type": "strength",
        "start_date": (now - timedelta(days=1)).isoformat(),
        "end_date": (now + timedelta(days=9)).isoformat(),
        "target_value": 10, "unit": "sets"}, content_type=JSON)
    assert made.status_code == 201, made.content
    return made.json()["id"]


def _rows(api, person):
    body = api(person).get("/api/social/challenges/").json()
    return {row["id"]: row for row in (body["results"] if isinstance(body, dict)
                                       else body)}


def test_s189_a_challenge_an_operator_hides_leaves_every_cached_list_at_once(
        make_user, api, rf):
    from social.models import Challenge
    from training_platform.admin_utils import bulk_apply

    creator = make_user("s189_creator")
    reader = make_user("s189_reader")
    operator = make_user("s189_op", user_type="admin", is_staff=True,
                         is_superuser=True)
    challenge_id = _challenge(api, creator, "s189 to hide")
    assert challenge_id in _rows(api, reader)          # now cached for the reader

    # The console's hide action, as it runs it (row by row through `save()`).
    request = rf.post("/dj-admin/")
    request.user = operator
    bulk_apply(request, Challenge.objects.filter(pk=challenge_id),
               "Hidden by moderation", is_hidden=True)

    assert api(reader).get(f"/api/social/challenges/{challenge_id}/").status_code == 404
    assert challenge_id not in _rows(api, reader)
    assert challenge_id not in _rows(api, creator)

    bulk_apply(request, Challenge.objects.filter(pk=challenge_id),
               "Unhidden by moderation", is_hidden=False)
    assert challenge_id in _rows(api, reader)


def test_s194_a_join_an_edit_a_close_and_a_delete_reach_everyone_elses_list(
        make_user, api):
    creator = make_user("s194_creator")
    reader = make_user("s194_reader")
    joiner = make_user("s194_joiner")
    challenge_id = _challenge(api, creator, "s194 before")
    row = _rows(api, reader)[challenge_id]                # cached for the reader
    assert (row["participants_count"], row["title"]) == (0, "s194 before")

    # Someone else joins: the reader's list counts them.
    joined = api(joiner).post(f"/api/social/challenges/{challenge_id}/join/", {},
                              content_type=JSON)
    assert joined.status_code == 201, joined.content
    assert _rows(api, reader)[challenge_id]["participants_count"] == 1

    # ...and leaves.
    left = api(joiner).post(f"/api/social/challenges/{challenge_id}/leave/", {},
                            content_type=JSON)
    assert left.status_code == 200, left.content
    assert _rows(api, reader)[challenge_id]["participants_count"] == 0

    # The creator renames it.
    renamed = api(creator).patch(f"/api/social/challenges/{challenge_id}/",
                                 {"title": "s194 after"}, content_type=JSON)
    assert renamed.status_code == 200, renamed.content
    assert _rows(api, reader)[challenge_id]["title"] == "s194 after"

    # The creator closes it.
    closed = api(creator).patch(f"/api/social/challenges/{challenge_id}/",
                                {"status": "completed"}, content_type=JSON)
    assert closed.status_code == 200, closed.content
    assert _rows(api, reader)[challenge_id]["status"] == "completed"

    # A new challenge reaches everyone's list too.
    second = _challenge(api, creator, "s194 second")
    assert second in _rows(api, reader)

    # The creator deletes the first.
    gone = api(creator).delete(f"/api/social/challenges/{challenge_id}/")
    assert gone.status_code == 204, gone.content
    assert challenge_id not in _rows(api, reader)


def test_s194_the_hourly_phase_task_reaches_every_cached_list(make_user, api):
    """A challenge starting is the phase task's `.update()`, which no signal sees."""
    from social.models import Challenge
    from social.tasks import advance_challenge_phases

    creator = make_user("s194p_creator")
    reader = make_user("s194p_reader")
    challenge_id = _challenge(api, creator, "s194 phases")
    Challenge.objects.filter(pk=challenge_id).update(status="upcoming")
    assert challenge_id in _rows(api, reader)
    Challenge.objects.filter(pk=challenge_id).update(
        end_date=timezone.now() - timedelta(minutes=5))   # cached copy says "active"
    assert _rows(api, reader)[challenge_id]["status"] == "active"
    advance_challenge_phases()
    assert _rows(api, reader)[challenge_id]["status"] == "completed"
