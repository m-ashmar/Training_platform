"""BACKEND_BUGS B-119, B-120, B-121, B-122, B-123 (2026-09-24), one section per row.

  * B-119 `GET /api/v1/ops/queue-counts/` answers the operator's four queues — transfer
    reviews to match, trainer verifications, community reports, withdrawals to process —
    one COUNT each, with the console page of each queue; anyone who is not a console
    operator gets 403 `admins_only`.
  * B-120 the operator's phone shows a two-minute, single-use sign-in link for the web
    console (`OperatorPhone`). The link alone signs nobody in: the computer that opens it
    shows a number, the SAME signed-in phone types it, and only then is that computer —
    the browser that opened the link, no other — given a console session, once. Behind
    `OPS_QR_LOGIN_ENABLED`, off by default: every route answers
    `console_login_disabled` while it is off.
  * B-121 `GET /api/auth/user/details/` carries `user_type`, the person's `media` map and
    an absolute `profile_picture`, and has its contract docstring.
  * B-122 a negative `rest_time` is refused with `rest_time_out_of_range` on `POST
    set-logs/`, `PATCH set-logs/{id}/` and every `set-logs/sync/` row.
  * B-123 each `sets[]` row of `GET workout-sessions/{id}/?expand=sets` carries
    `previous_best` (as stored on the set, null when it is not a record) and
    `client_uuid`.
"""
import json
import uuid
from datetime import timedelta

import pytest
from cachalot.api import cachalot_disabled
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
COUNTS = "/api/v1/ops/queue-counts/"
LOGIN = "/api/v1/ops/console-login/"
DETAILS = "/api/auth/user/details/"
SYNC = "/api/routine/set-logs/sync/"
SET_LOGS = "/api/routine/set-logs/"
SESSIONS = "/api/routine/workout-sessions/"


def _operator(make_user, tag, **fields):
    """A console operator: an `admin` account (staff and superuser, as `make_user`
    builds one) whose address was verified by the OTP flow."""
    return make_user(f"ops_{tag}", user_type="admin", email_verified=True, **fields)


# ============================================================ B-119 the four queues
def _counts(api, user):
    resp = api(user).get(COUNTS)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body, {row["key"]: row["count"] for row in body["queues"]}


def _seed_queues(make_user):
    """Two open transfer reviews and a decided one; one pending verification and a
    decided one; an open and a reviewing report and a dismissed one; a requested, an
    approved and a sent withdrawal, and one that arrived."""
    from moderation.models import Report
    from payouts.models import Withdrawal
    from subscription.models import TransferReview
    from trainer_verification.models import VerificationApplication

    now = timezone.now()
    for status in ("open", "open", "approved"):
        TransferReview.objects.create(transaction_id=f"b119-{uuid.uuid4()}", amount=1000,
                                      currency="SYP", reason="no_candidate", status=status)
    trainers = [make_user(f"b119_t{index}", user_type="trainer") for index in range(6)]
    VerificationApplication.objects.create(trainer=trainers[0], state="pending", round=1,
                                           submitted_at=now)
    VerificationApplication.objects.create(trainer=trainers[1], state="verified", round=1,
                                           submitted_at=now, decided_at=now)
    reporter = make_user("b119_reporter")
    for target, status in zip(trainers, ("open", "reviewing", "dismissed")):
        Report.objects.create(reporter=reporter, target_type="user", target_id=target.pk,
                              reason="spam", status=status)
    for index, state in enumerate(("requested", "approved", "sent", "confirmed")):
        Withdrawal.objects.create(number=f"WD-b119-{index}", trainer=trainers[index + 2],
                                  amount=100, currency="SYP", state=state,
                                  destination_kind="shamcash")


def test_b119_an_operator_reads_the_four_queue_counts_and_their_console_pages(
        make_user, api):
    operator = _operator(make_user, "counts")
    _, before = _counts(api, operator)
    _seed_queues(make_user)
    body, after = _counts(api, operator)

    # The design's order: payments to match, verifications, reports, withdrawals.
    assert [row["key"] for row in body["queues"]] == [
        "transfer_reviews", "trainer_verifications", "community_reports", "withdrawals"]
    grew = {key: after[key] - before[key] for key in after}
    assert grew == {"transfer_reviews": 2, "trainer_verifications": 1,
                    "community_reports": 2, "withdrawals": 3}, grew
    assert body["total"] == sum(after.values())

    pages = {row["key"]: row["console_url"] for row in body["queues"]}
    assert all(url.startswith("http") for url in pages.values()), pages
    assert "/dj-admin/subscription/transferreview/" in pages["transfer_reviews"]
    assert "status__exact=open" in pages["transfer_reviews"]
    assert "/dj-admin/trainer_verification/verificationqueueentry/" in \
        pages["trainer_verifications"]
    assert "/dj-admin/moderation/report/" in pages["community_reports"]
    assert "/dj-admin/payouts/withdrawalqueueentry/" in pages["withdrawals"]
    assert body["console_url"].startswith("http") and body["console_url"].endswith(
        "/dj-admin/")

    # An operator who is an admin by role but not a superuser reads them too: the
    # console's queues let any active admin in.
    role_only = _operator(make_user, "roleonly")
    role_only.is_superuser = False
    role_only.save()
    assert _counts(api, role_only)[1] == after


def test_b119_one_count_query_per_queue(make_user, api):
    operator = _operator(make_user, "cheap")
    _seed_queues(make_user)
    client = api(operator)
    with cachalot_disabled(), CaptureQueriesContext(connection) as queries:
        resp = client.get(COUNTS)
    assert resp.status_code == 200, resp.content
    counts = [q["sql"] for q in queries.captured_queries if "COUNT(" in q["sql"].upper()]
    assert len(counts) == 4, counts
    assert len(queries.captured_queries) <= 10, [q["sql"] for q in queries.captured_queries]


def test_b119_anyone_who_is_not_a_console_operator_is_refused_by_name(make_user, api):
    from admin_dashboard.ops_api import QueueCountsView

    staff_client = make_user("b119_staff", is_staff=True)       # console, not an admin
    for user in (make_user("b119_client"), make_user("b119_trainer", user_type="trainer"),
                 make_user("b119_agent", user_type="agent"), staff_client):
        resp = api(user).get(COUNTS)
        assert resp.status_code == 403, (user.username, resp.content)
        assert resp.json()["code"] == "admins_only", resp.json()

    inactive = _operator(make_user, "gone")
    inactive.is_active = False
    inactive.save()
    assert api(inactive).get(COUNTS).status_code in (401, 403)
    assert api().get(COUNTS).status_code == 401

    doc = QueueCountsView.get.__doc__ or QueueCountsView.__doc__
    for word in ("admins_only", "console_url", "transfer_reviews", "withdrawals"):
        assert word in doc, word


# ===================================================== B-120 the console sign-in link
def _desktop_status(token):
    return f"/dj-admin/qr-login/{token}/status/"


def _issue(api, operator):
    resp = api(operator).post(LOGIN, content_type=JSON)
    assert resp.status_code == 201, resp.content
    return resp.json()


def _token_of(body):
    """The link's secret, as the QR carries it: the path segment after `qr-login/`."""
    return body["url"].rstrip("/").rsplit("/", 1)[-1]


def _claim(token):
    """A computer opens the link and presses Continue. Returns (browser, page)."""
    browser = Client()
    landing = browser.get(f"/dj-admin/qr-login/{token}/")
    assert landing.status_code == 200, landing.content
    page = browser.post(f"/dj-admin/qr-login/{token}/")
    assert page.status_code == 200, page.content
    return browser, page


@pytest.fixture
def qr_on(settings):
    settings.OPS_QR_LOGIN_ENABLED = True
    return settings


def test_b120_everything_is_refused_by_name_while_the_setting_is_off(make_user, api,
                                                                    settings):
    settings.OPS_QR_LOGIN_ENABLED = False
    operator = _operator(make_user, "off")
    phone = api(operator)
    some_id = uuid.uuid4()

    for method, path in (("post", LOGIN), ("get", f"{LOGIN}{some_id}/"),
                         ("post", f"{LOGIN}{some_id}/approve/"),
                         ("post", f"{LOGIN}{some_id}/cancel/")):
        resp = getattr(phone, method)(path, content_type=JSON)
        assert resp.status_code == 403, (path, resp.content)
        assert resp.json()["code"] == "console_login_disabled", (path, resp.json())

    browser = Client()
    assert browser.get("/dj-admin/qr-login/abc/").status_code == 404
    assert browser.post("/dj-admin/qr-login/abc/").status_code == 404
    off = browser.post(_desktop_status("abc"))
    assert off.status_code == 404
    assert off.json()["code"] == "console_login_disabled"

    from admin_dashboard.models import ConsoleLogin
    assert not ConsoleLogin.objects.filter(user=operator).exists()
    assert "_auth_user_id" not in browser.session


def test_b120_the_phone_approves_the_number_the_computer_shows_and_that_computer_is_in(
        make_user, api, qr_on):
    from django.contrib.admin.models import LogEntry

    from admin_dashboard.models import ConsoleLogin

    operator = _operator(make_user, "happy")
    phone = api(operator)
    issued = _issue(api, operator)
    assert issued["state"] == "issued"
    assert issued["expires_in"] == 120
    assert "/dj-admin/qr-login/" in issued["url"] and issued["url"].startswith("http")
    token = _token_of(issued)
    ticket = ConsoleLogin.objects.get(pk=issued["id"])
    assert ticket.token_digest and token not in ticket.token_digest, \
        "the link's secret is stored raw"

    # Opening the link changes nothing until Continue is pressed (a mail scanner that
    # follows links must not spend it).
    browser = Client()
    assert browser.get(f"/dj-admin/qr-login/{token}/").status_code == 200
    assert phone.get(f"{LOGIN}{issued['id']}/").json()["state"] == "issued"

    page = browser.post(f"/dj-admin/qr-login/{token}/")
    assert page.status_code == 200, page.content
    ticket.refresh_from_db()
    assert ticket.state == "claimed" and len(ticket.match_code) == 3
    assert ticket.match_code in page.content.decode()
    read = phone.get(f"{LOGIN}{issued['id']}/").json()
    assert read["state"] == "claimed"
    assert "match_code" not in json.dumps(read) and ticket.match_code not in json.dumps(read)

    # Waiting for the phone: nobody is signed in.
    waiting = browser.post(_desktop_status(token))
    assert waiting.status_code == 200 and waiting.json()["state"] == "waiting"
    assert "_auth_user_id" not in browser.session

    wrong = str((int(ticket.match_code) % 900) + 100)
    miss = phone.post(f"{LOGIN}{issued['id']}/approve/", json.dumps({"code": wrong}),
                      content_type=JSON)
    assert miss.status_code == 400, miss.content
    assert (miss.json()["code"], miss.json()["attempts_left"]) == (
        "console_login_code_mismatch", 2)

    ok = phone.post(f"{LOGIN}{issued['id']}/approve/",
                    json.dumps({"code": ticket.match_code}), content_type=JSON)
    assert ok.status_code == 200, ok.content
    assert ok.json()["state"] == "approved"

    done = browser.post(_desktop_status(token))
    assert done.status_code == 200, done.content
    assert done.json()["state"] == "signed_in"
    assert done.json()["next"].endswith("/dj-admin/")
    assert browser.session["_auth_user_id"] == str(operator.pk)
    assert browser.get("/dj-admin/").status_code == 200

    ticket.refresh_from_db()
    assert ticket.state == "used" and ticket.used_at is not None
    assert phone.get(f"{LOGIN}{issued['id']}/").json()["state"] == "used"
    assert LogEntry.objects.filter(user=operator, object_id=str(ticket.pk)).exists(), \
        "the sign-in left no line in the console's own log"


def test_b120_the_link_alone_signs_nobody_in(make_user, api, qr_on):
    """Whoever holds the link — a forwarded mail, a photographed QR — reaches a page
    with a number on it and nothing more, and the two minutes then run out."""
    from admin_dashboard.models import ConsoleLogin

    operator = _operator(make_user, "alone")
    issued = _issue(api, operator)
    token = _token_of(issued)
    stranger, _page = _claim(token)
    for _ in range(3):
        assert stranger.post(_desktop_status(token)).json()["state"] == "waiting"
    assert "_auth_user_id" not in stranger.session

    ConsoleLogin.objects.filter(pk=issued["id"]).update(
        expires_at=timezone.now() - timedelta(seconds=1))
    assert stranger.post(_desktop_status(token)).json()["state"] == "expired"
    late = api(operator).post(f"{LOGIN}{issued['id']}/approve/",
                              json.dumps({"code": ConsoleLogin.objects.get(
                                  pk=issued["id"]).match_code}), content_type=JSON)
    assert late.status_code == 410 and late.json()["code"] == "console_login_expired"
    assert stranger.post(_desktop_status(token)).json()["state"] == "expired"
    assert "_auth_user_id" not in stranger.session
    # An expired link is a dead page, not a way back in.
    assert Client().get(f"/dj-admin/qr-login/{token}/").status_code == 410


def test_b120_only_the_browser_that_opened_the_link_is_signed_in_and_only_once(
        make_user, api, qr_on):
    from admin_dashboard.models import ConsoleLogin

    operator = _operator(make_user, "bound")
    phone = api(operator)
    issued = _issue(api, operator)
    token = _token_of(issued)
    browser, _page = _claim(token)

    # A second computer cannot open the same link once it is opened.
    other = Client()
    assert other.post(f"/dj-admin/qr-login/{token}/").status_code == 410
    code = ConsoleLogin.objects.get(pk=issued["id"]).match_code
    assert phone.post(f"{LOGIN}{issued['id']}/approve/", json.dumps({"code": code}),
                      content_type=JSON).status_code == 200

    # Approved — and still nobody else gets in on it.
    elsewhere = other.post(_desktop_status(token))
    assert elsewhere.status_code == 403, elsewhere.content
    assert elsewhere.json()["code"] == "console_login_wrong_browser"
    assert "_auth_user_id" not in other.session
    cookie = browser.cookies["ops_qr_login"].value
    thief = Client()
    thief.cookies["ops_qr_login"] = ("y" if cookie[0] == "x" else "x") + cookie[1:]
    assert thief.post(_desktop_status(token)).status_code == 403

    assert browser.post(_desktop_status(token)).json()["state"] == "signed_in"
    # One use: the same cookie, replayed from anywhere, signs in nobody.
    replay = Client()
    replay.cookies["ops_qr_login"] = cookie
    again = replay.post(_desktop_status(token))
    assert again.status_code in (200, 403), again.content
    assert again.status_code == 403 or again.json()["state"] == "used"
    assert "_auth_user_id" not in replay.session


def test_b120_approving_needs_a_computer_the_right_number_and_the_issuing_phone(
        make_user, api, qr_on):
    from admin_dashboard.models import ConsoleLogin

    operator = _operator(make_user, "rules")
    phone = api(operator)
    issued = _issue(api, operator)
    approve = f"{LOGIN}{issued['id']}/approve/"

    early = phone.post(approve, json.dumps({"code": "123"}), content_type=JSON)
    assert early.status_code == 409 and early.json()["code"] == "console_login_not_claimed"

    token = _token_of(issued)
    browser, _page = _claim(token)
    blank = phone.post(approve, json.dumps({}), content_type=JSON)
    assert blank.status_code == 400 and blank.json()["code"] == "console_login_code_required"

    # Another operator cannot see or approve it.
    colleague = api(_operator(make_user, "colleague"))
    assert colleague.get(f"{LOGIN}{issued['id']}/").json()["code"] == \
        "console_login_not_found"
    code = ConsoleLogin.objects.get(pk=issued["id"]).match_code
    theirs = colleague.post(approve, json.dumps({"code": code}), content_type=JSON)
    assert theirs.status_code == 404 and theirs.json()["code"] == "console_login_not_found"

    # Three wrong numbers end it; the right one afterwards does nothing.
    wrong = str((int(code) % 900) + 100)
    answers = [phone.post(approve, json.dumps({"code": wrong}), content_type=JSON).json()
               for _ in range(3)]
    assert [a["attempts_left"] for a in answers] == [2, 1, 0]
    assert answers[-1]["state"] == "cancelled"
    closed = phone.post(approve, json.dumps({"code": code}), content_type=JSON)
    assert closed.status_code == 409 and closed.json()["code"] == "console_login_closed"
    assert browser.post(_desktop_status(token)).json()["state"] == "cancelled"
    assert "_auth_user_id" not in browser.session


def test_b120_the_phone_can_cancel_and_a_new_link_replaces_the_old_one(
        make_user, api, qr_on):
    operator = _operator(make_user, "cancel")
    phone = api(operator)
    first = _issue(api, operator)
    browser, _page = _claim(_token_of(first))
    cancelled = phone.post(f"{LOGIN}{first['id']}/cancel/", content_type=JSON)
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert browser.post(_desktop_status(_token_of(first))).json()["state"] == "cancelled"

    second = _issue(api, operator)
    third = _issue(api, operator)
    assert phone.get(f"{LOGIN}{second['id']}/").json()["state"] == "cancelled"
    assert phone.get(f"{LOGIN}{third['id']}/").json()["state"] == "issued"
    assert Client().get(f"/dj-admin/qr-login/{_token_of(second)}/").status_code == 410


def test_b120_only_a_verified_console_operator_may_make_a_link_and_it_is_rate_limited(
        make_user, api, qr_on):
    from admin_dashboard.models import ConsoleLogin

    for user in (make_user("b120_client"), make_user("b120_trainer", user_type="trainer"),
                 make_user("b120_agent", user_type="agent"),
                 make_user("b120_staff", is_staff=True, email_verified=True)):
        resp = api(user).post(LOGIN, content_type=JSON)
        assert resp.status_code == 403 and resp.json()["code"] == "admins_only", \
            (user.username, resp.content)
    unverified = make_user("ops_unverified", user_type="admin", email_verified=False)
    resp = api(unverified).post(LOGIN, content_type=JSON)
    assert resp.status_code == 403 and resp.json()["code"] == "email_not_verified"
    assert api().post(LOGIN, content_type=JSON).status_code == 401

    operator = _operator(make_user, "busy")
    for _ in range(5):
        _issue(api, operator)
    limited = api(operator).post(LOGIN, content_type=JSON)
    assert limited.status_code == 429, limited.content
    assert limited.json()["code"] == "throttled"
    assert limited.json()["retry_after_seconds"] > 0
    assert ConsoleLogin.objects.filter(user=operator).count() == 5


def test_b120_an_operator_who_lost_the_console_before_the_computer_asks_is_not_let_in(
        make_user, api, qr_on):
    from admin_dashboard.models import ConsoleLogin

    operator = _operator(make_user, "demoted")
    issued = _issue(api, operator)
    token = _token_of(issued)
    browser, _page = _claim(token)
    code = ConsoleLogin.objects.get(pk=issued["id"]).match_code
    assert api(operator).post(f"{LOGIN}{issued['id']}/approve/", json.dumps({"code": code}),
                              content_type=JSON).status_code == 200
    operator.user_type = "client"
    operator.is_staff = operator.is_superuser = False
    operator.save()
    assert browser.post(_desktop_status(token)).json()["state"] == "cancelled"
    assert "_auth_user_id" not in browser.session


def test_b120_the_views_carry_their_contract():
    from admin_dashboard import ops_api

    doc = " ".join((view.__doc__ or "") + " ".join(
        (getattr(view, m).__doc__ or "") for m in ("get", "post") if hasattr(view, m))
        for view in (ops_api.ConsoleLoginView, ops_api.ConsoleLoginDetailView,
                     ops_api.ConsoleLoginApproveView, ops_api.ConsoleLoginCancelView))
    for code in ("console_login_disabled", "admins_only", "email_not_verified",
                 "throttled", "console_login_not_found", "console_login_not_claimed",
                 "console_login_code_required", "console_login_code_mismatch",
                 "console_login_expired", "console_login_closed", "OPS_QR_LOGIN_ENABLED"):
        assert code in doc, code


# ================================================================ B-121 user details
def test_b121_user_details_names_the_role_the_media_and_an_absolute_picture(
        make_user, api):
    from mediakit.models import MediaAsset
    from users.views import UserDetailsView

    agent = make_user("b121_agent", user_type="agent", first_name="Samer")
    agent.profile_picture.name = "profile_pictures/b121_agent.png"
    agent.save()
    body = api(agent).get(DETAILS).json()
    # The keys the app reads today are where they were.
    assert (body["id"], body["username"], body["first_name"]) == (
        agent.pk, "b121_agent", "Samer")
    assert body["email"] == agent.email and "last_name" in body
    assert body["user_type"] == "agent"
    assert isinstance(body["profile_picture"], str)
    assert body["profile_picture"].startswith("http"), body["profile_picture"]
    assert "b121_agent" in body["profile_picture"]
    assert body["media"] == {}

    bare = make_user("b121_bare")
    body = api(bare).get(DETAILS).json()
    assert (body["user_type"], body["profile_picture"], body["media"]) == (
        "client", None, {})

    trainer = make_user("b121_trainer", user_type="trainer")
    MediaAsset.objects.create(owner_type="trainer", owner_id=trainer.pk, person=trainer,
                              slot="trainer.avatar", original="k.jpg", file_1x="k1.webp",
                              file_2x="k2.webp")
    body = api(trainer).get(DETAILS).json()
    assert body["user_type"] == "trainer"
    assert set(body["media"]) == {"trainer.avatar"}
    assert {"url_1x", "url_2x"} <= set(body["media"]["trainer.avatar"])

    assert api().get(DETAILS).json()["code"] == "not_authenticated"
    doc = UserDetailsView.get.__doc__
    for word in ("Authorization", "user_type", "media", "profile_picture",
                 "not_authenticated", "token_not_valid"):
        assert word in doc, word


# ===================================================== B-122 a rest is never negative
def _program(make_user, tag):
    from routine.models import Exercise, Routine, RoutineExercise

    trainer = make_user(f"orow_t_{tag}", user_type="trainer")
    client = make_user(f"orow_c_{tag}")
    routine = Routine.objects.create(name=f"orow {tag}", created_by=trainer, days=1)
    exercise = Exercise.objects.create(name=f"orow ex {tag}", created_by=trainer)
    row = RoutineExercise.objects.create(routine=routine, exercise=exercise, day=1,
                                         order=1, sets=4, reps=8, weight=82.5,
                                         rest_time=120)
    routine.assigned_to.add(client)
    return trainer, client, routine, row


def _row(exercise, **fields):
    return {"client_uuid": str(uuid.uuid4()), "exercise_id": exercise.pk,
            "set_number": 1, "reps": 8, **fields}


def _sync(api, user, rows, commit):
    with commit(execute=True):
        resp = api(user).post(SYNC, json.dumps({"sets": rows}), content_type=JSON)
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_b122_a_negative_rest_is_refused_by_name_on_every_route(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog, UserExerciseProgress

    commit = django_capture_on_commit_callbacks
    _trainer, client, _routine, row = _program(make_user, "rest")
    exercise = row.exercise
    batch = [_row(exercise, set_number=1, rest_time=-5),
             _row(exercise, set_number=2, rest_time=0),
             _row(exercise, set_number=3, rest_time=90)]
    body = _sync(api, client, batch, commit)
    assert [r.get("code") for r in body["results"]] == ["rest_time_out_of_range", None,
                                                          None], body
    assert not ExerciseSetLog.objects.filter(client_uuid=batch[0]["client_uuid"]).exists()
    assert ExerciseSetLog.objects.get(client_uuid=batch[1]["client_uuid"]).rest_time == 0

    progress = UserExerciseProgress.objects.get(user=client, exercise=exercise,
                                                date=timezone.localdate())
    with commit(execute=True):
        refused = api(client).post(SET_LOGS, {"user_exercise_progress": progress.pk,
                                              "set_number": 7, "weight": 40, "reps": 8,
                                              "rest_time": -1}, content_type=JSON)
    assert refused.status_code == 400, refused.content
    assert refused.json()["field_errors"]["rest_time"][0]["code"] == \
        "rest_time_out_of_range", refused.json()
    assert not ExerciseSetLog.objects.filter(user_exercise_progress=progress,
                                             set_number=7).exists()

    stored = ExerciseSetLog.objects.get(client_uuid=batch[2]["client_uuid"])
    patched = api(client).patch(f"{SET_LOGS}{stored.pk}/", {"rest_time": -30},
                                content_type=JSON)
    assert patched.status_code == 400, patched.content
    assert patched.json()["field_errors"]["rest_time"][0]["code"] == \
        "rest_time_out_of_range"
    stored.refresh_from_db()
    assert stored.rest_time == 90


# ============================================ B-123 what a reviewed record beat
def test_b123_each_reviewed_set_carries_its_client_uuid_and_the_best_it_beat(
        make_user, api, django_capture_on_commit_callbacks):
    from routine.models import ExerciseSetLog, UserExerciseProgress, WorkoutSession
    from routine.views import WorkoutSessionViewSet
    from users.models import TrainerClientRelation

    commit = django_capture_on_commit_callbacks
    trainer, client, routine, row = _program(make_user, "review")
    earlier = timezone.localdate() - timedelta(days=7)
    progress, _ = UserExerciseProgress.objects.get_or_create(
        user=client, exercise=row.exercise, date=earlier, defaults={"target_sets": 4})
    ExerciseSetLog.objects.create(user_exercise_progress=progress, set_number=1,
                                  weight=80, reps=8, date=earlier)
    began = timezone.now() - timedelta(hours=1)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1,
                                            start_time=began, status="completed",
                                            end_time=began + timedelta(minutes=40))
    sent = [_row(row.exercise, set_number=number, weight=weight, reps=reps,
                 workout_session_id=session.pk)
            for number, (weight, reps) in enumerate(((82.5, 8), (82.5, 7), (80, 8)),
                                                    start=1)]
    answers = {r["client_uuid"]: r for r in _sync(api, client, sent, commit)["results"]}
    assert answers[sent[0]["client_uuid"]]["personal_record"] is True
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")

    resp = api(trainer).get(f"{SESSIONS}{session.pk}/?expand=sets")
    assert resp.status_code == 200, resp.content
    sets = resp.json()["sets"]
    assert [s["client_uuid"] for s in sets] == [r["client_uuid"] for r in sent]
    assert sets[0]["personal_record"] is True
    assert sets[0]["previous_best"] == {"weight": 80.0, "reps": 8,
                                        "date": earlier.isoformat()}
    assert sets[0]["previous_best"] == answers[sent[0]["client_uuid"]]["previous_best"]
    assert [s["previous_best"] for s in sets[1:]] == [None, None]

    # As stored on the set, never recomputed: an earlier set corrected since does not
    # change what the record beat.
    ExerciseSetLog.objects.filter(user_exercise_progress=progress).update(weight=70)
    again = api(client).get(f"{SESSIONS}{session.pk}/?expand=sets").json()["sets"]
    assert again[0]["previous_best"] == sets[0]["previous_best"]

    # A set logged without a key of the app's answers null, not a made-up one.
    legacy = ExerciseSetLog.objects.get(client_uuid=sent[2]["client_uuid"])
    ExerciseSetLog.objects.filter(pk=legacy.pk).update(client_uuid=None)
    last = api(client).get(f"{SESSIONS}{session.pk}/?expand=sets").json()["sets"][-1]
    assert (last["id"], last["client_uuid"]) == (legacy.pk, None)

    doc = WorkoutSessionViewSet.retrieve.__doc__
    assert "previous_best" in doc and "client_uuid" in doc
