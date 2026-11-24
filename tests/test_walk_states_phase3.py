"""The phase 3 worker's walk states (`users/walk/community.py`, `inbox.py`,
`operator.py`, `progress.py`): each made by `seed_walk_states --only <state>`, and each
checked through the API the screen reads.
"""
import pytest

from tests.walk_fixtures import walk_platform, walk_run  # noqa: F401  (the shared fixtures)

pytestmark = pytest.mark.django_db

FALLBACK = "You have a new notification."


def _user(entry, part="main"):
    from users.models import CustomUser

    return CustomUser.objects.get(pk=entry["accounts"][part]["id"])


def _rows(body):
    return body["results"] if isinstance(body, dict) and "results" in body else body


# ------------------------------------------------------------------ 1. challenges
def test_client_challenges(walk_run, api):
    from django.utils import timezone

    entry = walk_run("client_challenges")
    me = _user(entry)
    ids = entry["ids"]
    assert len(ids["hosted"]) == 3

    # The host limit: a fourth is refused, with the limit and what they host.
    fourth = api(me).post("/api/social/challenges/", {
        "title": "رابع", "description": "x", "challenge_type": "workout",
        "start_date": timezone.now().isoformat(),
        "end_date": (timezone.now() + timezone.timedelta(days=5)).isoformat(),
        "target_value": 5, "unit": "", "metric": "manual"}, content_type="application/json")
    assert fourth.status_code == 409 and fourth.json()["code"] == "challenge_host_limit"
    assert (fourth.json()["limit"], fourth.json()["hosting"]) == (3, 3)

    # In one of each newer kind, with progress, each read from its own page.
    expected = {"exercise_reps": 90.0, "streak_days": 3.0, "training_days": 3.0,
                "my_clients": 9.0}
    for kind, value in expected.items():
        detail = api(me).get(f"/api/social/challenges/{ids[f'challenge_{kind}']}/").json()
        assert detail["user_progress"]["current_value"] == value, (kind, detail)
        assert detail["auto_counted"] is True
    clients_only = api(me).get(f"/api/social/challenges/{ids['challenge_my_clients']}/")
    assert clients_only.json()["audience"] == "my_clients"


# ------------------------------------------------------------------ 2. reports
def test_client_reports(walk_run, api):
    entry = walk_run("client_reports")
    me = _user(entry)
    ids = entry["ids"]
    mine = _rows(api(me).get("/api/v1/reports/mine/").json())
    assert {row["status"] for row in mine} == {"open", "reviewing", "actioned", "dismissed"}
    by_id = {row["id"]: row for row in mine}
    for status in ("open", "reviewing", "actioned", "dismissed"):
        assert by_id[ids[f"report_{status}"]]["status"] == status
    assert by_id[ids["report_actioned"]]["handled_at"]

    posts = _rows(api(me).get(f"/api/social/posts/?author={me.pk}").json())
    assert ids["post"] in {row["id"] for row in posts}
    comments = _rows(api(me).get(f"/api/social/comments/?post={ids['their_post']}").json())
    assert ids["comment"] in {row["id"] for row in comments}


# ------------------------------------------------------------------ 3. inbox
def _inbox(api, user, build):
    rows = api(user).get("/api/social/notifications/?page_size=100",
                         HTTP_X_APP_CHANNEL=build, HTTP_ACCEPT_LANGUAGE="en").json()
    return {row["notification_type"]: row for row in _rows(rows)}


def test_inbox_every_kind(walk_run, api):
    from users.walk.inbox import kinds_for

    entry = walk_run("inbox_every_kind")
    me, coach = _user(entry), _user(entry, "coach")

    mine = _inbox(api, me, "apk")
    assert set(mine) == set(kinds_for("client"))
    assert not [k for k, row in mine.items() if row["message"] == FALLBACK or not row["title"]]
    assert {"checkout_expired", "subscription_expired", "coaching_payment_due"} <= set(mine)

    # The three build-gated ones, as the store builds read them.
    ios = _inbox(api, me, "ios")
    assert "checkout_expired" not in ios
    assert ios["coaching_payment_due"]["message"] == \
        "Your coaching with هادي is paused. You can still see your routines."
    assert ios["subscription_expired"]["message"] == \
        "Your diet plan and routine are saved and you can still see them."

    theirs = _inbox(api, coach, "apk")
    assert set(theirs) == set(kinds_for("trainer"))
    assert not [k for k, row in theirs.items() if row["message"] == FALLBACK]
    assert set(entry["ids"]["trainer_kinds"]) == set(kinds_for("trainer"))


# ------------------------------------------------------------------ 4. the operator
def test_operator_staff(walk_run, api):
    entry = walk_run("operator_staff")
    operator = _user(entry)
    assert entry["role"] == "admin"
    counts = api(operator).get("/api/v1/ops/queue-counts/")
    assert counts.status_code == 200, counts.content


# ------------------------------------------------------------------ 5. progress
def test_client_progress(walk_run, api):
    entry = walk_run("client_progress")
    me = _user(entry)

    progress = {row["achievement"]["key"]: row
                for row in api(me).get("/api/achievements/progress/").json()["progress"]}
    warrior = progress.get("workout_warrior")
    assert warrior and (warrior["current_value"], warrior["target_value"]) == (9, 10), progress

    sessions = _rows(api(me).get("/api/routine/workout-sessions/?status=completed").json())
    assert len([s for s in sessions if s["status"] == "completed"]) == 9

    latest = _rows(api(me).get(
        "/api/analytics/metrics/?latest_for=weight,chest,arm,waist,thigh").json())
    assert {row["metric_type"] for row in latest} == {"weight", "chest", "arm", "waist",
                                                     "thigh"}
    trend = api(me).get("/api/analytics/metrics/metric_trends/?metric_type=weight").json()
    assert trend, trend

    goals = _rows(api(me).get("/api/analytics/goals/").json())
    goal = next(g for g in goals if g["id"] == entry["ids"]["goal"])
    assert goal["progress_percentage"] == 50.0
