"""Every refusal in `users` and `mediakit` names its own code (the owner, 2026-09-24).

Decision 9: "Give generic API refusals their own specific error codes. Do it now;
finalize the contract before the Flutter integration gets deeper." These refusals named
no `code`, so the error envelope (`training_platform/middleware.py`
`normalize_error_envelope`) filled in the status default — `bad_request`,
`permission_denied`, `not_found`, `conflict` — and the app could not tell a caller of
the wrong role from a paywall, a missing field from a bad id, or one conflict from
another. API_CONTRACT.md §6 allows a status default to gain a code of its own; a code a
view already NAMED never changes.

Each test below asserts the new code for its refusal, through the real route.
"""
import ast
import pathlib
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

pytestmark = pytest.mark.django_db

AUTH = "/api/auth"
JSON = "application/json"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _code(response):
    return response.status_code, response.json()["code"]


# A deliberate, pre-release contract change (BACKEND_BUGS B-092, 2026-09-24): two codes
# minted here that day named refusals an OLDER code already named, so they were unified
# onto the older one before anything shipped (API_CONTRACT.md §6). The tests below still
# assert what they were written to protect — the refusal names its own code, not the
# status default — only the name of that code moved.
#: The caller is not a trainer: `not_a_trainer`, the routine and mediakit trainer routes'
#: code since before 4cbb522 (it was minted here as `trainers_only`).
NOT_A_TRAINER = "not_a_trainer"
#: An approved pair, asked again from the trainer's side: `already_with_this_trainer`,
#: the code the client's side answers for the same state (was `already_your_client`).
ALREADY_A_PAIR = "already_with_this_trainer"


# ------------------------------------------------------------------- sign-in and out
def test_logout_names_a_missing_and_a_bad_refresh_token(make_user, api):
    user = make_user("oc_logout")
    missing = api(user).post(f"{AUTH}/token/logout/", {}, content_type=JSON)
    assert _code(missing) == (400, "refresh_token_required"), missing.content
    assert missing.json()["field"] == "refresh_token"

    bad = api(user).post(f"{AUTH}/token/logout/", {"refresh_token": "not-a-token"},
                         content_type=JSON)
    assert _code(bad) == (400, "refresh_token_invalid"), bad.content


def test_the_code_routes_name_a_missing_field(api):
    verify = api().post(f"{AUTH}/verify-otp/", {"email": "oc@gate.test"},
                        content_type=JSON)
    assert _code(verify) == (400, "email_and_otp_required"), verify.content

    resend = api().post(f"{AUTH}/resend-otp/", {}, content_type=JSON)
    assert _code(resend) == (400, "email_required"), resend.content
    assert resend.json()["field"] == "email"


def test_a_wrong_password_names_its_reason_beside_validation_error(make_user, api):
    """Object-level login refusals stay `validation_error` (the regression gate pins
    that); the reason is the field code, which was DRF's generic `invalid`."""
    make_user("oc_login")
    wrong = api().post(f"{AUTH}/login/", {"email": "oc_login@gate.test",
                                          "password": "wrong-password-1"},
                       content_type=JSON)
    assert wrong.status_code == 400, wrong.content
    body = wrong.json()
    assert body["code"] == "validation_error"
    assert body["field_errors"]["non_field_errors"][0]["code"] == "invalid_credentials"


def test_an_unverified_address_is_told_so_by_code_on_both_sign_in_routes(make_user, api):
    make_user("oc_unverified", email_verified=False)
    creds = {"email": "oc_unverified@gate.test", "password": "Xx!23456"}
    for path in (f"{AUTH}/login/", f"{AUTH}/token/"):
        r = api().post(path, creds, content_type=JSON)
        assert r.status_code == 400, (path, r.content)
        body = r.json()
        assert body["field_errors"]["non_field_errors"][0]["code"] == "email_not_verified", \
            (path, body)
        assert "requires_verification" in body, (path, body)
        # `/token/` put the sentence under a `detail` key, which the error handler does
        # not read as a field, so the first "field" it found won: `detail: "True"`.
        assert "verify your email" in body["detail"], (path, body)


# ------------------------------------------------------------------ the wrong role
TRAINER_ONLY = [
    ("get", "trainer/profile/"), ("post", "trainer/profile/"),
    ("get", "trainer/clients/"), ("post", "trainer/assign-client/"),
    ("post", "trainer/unassign-client/"), ("get", "trainer/pending-requests/"),
    ("post", "trainer/respond-to-request/"), ("get", "trainer/client-profile/"),
]
CLIENT_ONLY = [
    ("get", "client/profile/"), ("post", "client/profile/"),
    ("get", "client/available-trainers/"), ("post", "client/request-trainer/"),
    ("post", "client/unassign-trainer/"), ("post", "client/cancel-trainer-request/"),
    ("post", "client/respond-to-invite/"), ("get", "client/trainer-invites/"),
    ("get", "client/request-status/"),
]


def test_a_route_for_one_role_names_the_role(make_user, api):
    """`permission_denied` is also the paywall's and an object's answer; the role
    refusal is `not_a_trainer` (see NOT_A_TRAINER) / `clients_only`, the codes the
    routine trainer routes, `coaching/current/` and `trainers/matches` already answer."""
    client = api(make_user("oc_role_client"))
    trainer = api(make_user("oc_role_trainer", user_type="trainer"))
    for method, path in TRAINER_ONLY:
        r = getattr(client, method)(f"{AUTH}/{path}", {}, content_type=JSON)
        assert _code(r) == (403, NOT_A_TRAINER), (path, r.content)
    for method, path in CLIENT_ONLY:
        r = getattr(trainer, method)(f"{AUTH}/{path}", {}, content_type=JSON)
        assert _code(r) == (403, "clients_only"), (path, r.content)


# ------------------------------------------------------- a trainer and their clients
def test_an_invitation_names_each_refusal(make_user, api):
    from users.models import TrainerClientRelation

    trainer = make_user("oc_inv_trainer", user_type="trainer")
    waiting = make_user("oc_inv_waiting")
    mine = make_user("oc_inv_mine")
    TrainerClientRelation.objects.create(trainer=trainer, client=mine, status="approved",
                                         initiated_by="client")
    t = api(trainer)

    missing = t.post(f"{AUTH}/trainer/assign-client/", {}, content_type=JSON)
    assert _code(missing) == (400, "client_id_required"), missing.content
    assert missing.json()["field"] == "client_id"

    assert t.post(f"{AUTH}/trainer/assign-client/", {"client_id": waiting.pk},
                  content_type=JSON).status_code == 200
    again = t.post(f"{AUTH}/trainer/assign-client/", {"client_id": waiting.pk},
                   content_type=JSON)
    assert _code(again) == (400, "request_already_pending"), again.content

    already = t.post(f"{AUTH}/trainer/assign-client/", {"client_id": mine.pk},
                     content_type=JSON)
    assert _code(already) == (400, ALREADY_A_PAIR), already.content

    nobody = t.post(f"{AUTH}/trainer/assign-client/", {"client_id": 987654321},
                    content_type=JSON)
    assert _code(nobody) == (404, "not_found"), nobody.content


def test_ending_a_coaching_that_is_not_yours_names_it(make_user, api):
    trainer = make_user("oc_end_trainer", user_type="trainer")
    stranger = make_user("oc_end_client")
    t = api(trainer)

    missing = t.post(f"{AUTH}/trainer/unassign-client/", {}, content_type=JSON)
    assert _code(missing) == (400, "client_id_required"), missing.content

    not_mine = t.post(f"{AUTH}/trainer/unassign-client/", {"client_id": stranger.pk},
                      content_type=JSON)
    assert _code(not_mine) == (400, "not_your_client"), not_mine.content


def test_reading_another_trainers_client_names_it(make_user, api):
    trainer = make_user("oc_read_trainer", user_type="trainer")
    other = make_user("oc_read_client")
    r = api(trainer).get(f"{AUTH}/trainer/client-profile/{other.pk}/")
    assert _code(r) == (403, "not_your_client"), r.content


def test_a_client_with_nothing_to_leave_or_cancel_is_told_which(make_user, api):
    c = api(make_user("oc_leave_client"))
    leave = c.post(f"{AUTH}/client/unassign-trainer/", {}, content_type=JSON)
    assert _code(leave) == (400, "no_assigned_trainer"), leave.content

    cancel = c.post(f"{AUTH}/client/cancel-trainer-request/", {}, content_type=JSON)
    assert _code(cancel) == (400, "no_pending_request"), cancel.content


# ----------------------------------------------------------------- password, device
def test_a_reset_token_names_unknown_and_spent(make_user, api):
    from users.models import PasswordResetToken

    user = make_user("oc_reset")
    body = {"new_password": "An0ther!Strong-pass"}
    unknown = api().post(f"{AUTH}/forgot-password/confirm/",
                         {**body, "reset_token": "no-such-token"}, content_type=JSON)
    assert _code(unknown) == (400, "reset_token_invalid"), unknown.content

    token, raw = PasswordResetToken.issue(user, minutes=15)
    token.is_used = True
    token.save(update_fields=["is_used"])
    spent = api().post(f"{AUTH}/forgot-password/confirm/",
                       {**body, "reset_token": raw}, content_type=JSON)
    assert _code(spent) == (400, "reset_token_expired"), spent.content


def test_registering_a_device_without_a_token_names_it(make_user, api):
    """The code `DELETE` already answers for the same missing field."""
    r = api(make_user("oc_device")).post(f"{AUTH}/device-token/", {}, content_type=JSON)
    assert _code(r) == (400, "token_required"), r.content
    assert r.json()["field"] == "token"


# ------------------------------------------------------------------------ mediakit
def _clip():
    return SimpleUploadedFile("clip.mp4", b"\x00" * 64, content_type="video/mp4")


def test_a_media_write_that_is_not_yours_names_it(make_user, api, settings, tmp_path):
    """Refused before the bytes are read, so any file does."""
    from routine.models import Exercise
    from users.models import TrainerClientRelation

    settings.MEDIA_ROOT = str(tmp_path)
    owner = make_user("oc_media_owner", user_type="trainer")
    other = make_user("oc_media_other", user_type="trainer")
    reader = make_user("oc_media_reader")
    TrainerClientRelation.objects.create(trainer=owner, client=reader, status="approved",
                                         initiated_by="client")
    private = Exercise.objects.create(name="oc owner press", created_by=owner)
    url = "/api/v1/content/media/video/"

    seen_not_owned = api(reader).post(url, {"owner_type": "exercise",
                                            "owner_id": private.pk,
                                            "slot": "exercise.cue", "file": _clip()})
    assert _code(seen_not_owned) == (403, "media_not_yours"), seen_not_owned.content

    someone_else = api(owner).post(url, {"owner_type": "trainer", "owner_id": other.pk,
                                         "slot": "trainer.intro", "file": _clip()})
    assert _code(someone_else) == (403, "media_not_yours"), someone_else.content

    wrong_route = api(owner).post(url, {"owner_type": "trainer_exercise",
                                        "owner_id": private.pk,
                                        "slot": "exercise.loop", "file": _clip()})
    assert _code(wrong_route) == (403, "media_use_trainer_route"), wrong_route.content


# ---------------------------------------------------------------------------- chat
def test_opening_a_thread_with_no_other_side_answers_its_documented_code(make_user, api):
    """`invalid_body` was raised as DRF's ValidationError, whose code never reaches the
    envelope: the app read `validation_error`, not the documented `invalid_body`."""
    r = api(make_user("oc_chat", user_type="trainer")).post(
        "/api/v1/chat/conversations/start/", {}, content_type=JSON)
    assert _code(r) == (400, "invalid_body"), r.content


# ------------------------------------------------------------ nothing left unnamed
VIEW_FILES = ("users/views.py", "users/device_token_views.py", "users/trainer_views.py",
              "users/coaching_views.py", "mediakit/views.py", "mediakit/trainer_views.py",
              "chat/views.py", "moderation/views.py", "ai_assistant/views.py",
              "achievements/views.py", "notifications/views.py",
              "notifications/trainer_message.py")

#: Bodies built by a helper that always sets `code`.
CODED_HELPERS = ("refusal_body(",)


def _status(call):
    for kw in call.keywords:
        if kw.arg == "status":
            return ast.unparse(kw.value)
    return ast.unparse(call.args[1]) if len(call.args) > 1 else ""


def test_every_4xx_response_in_these_apps_names_a_code():
    """A 4xx `Response` with no `code` in its body answers the status default. The
    owner's rule is that none does; this keeps the next one from slipping in."""
    unnamed = []
    for rel in VIEW_FILES:
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "Response"):
                continue
            if not re.search(r"HTTP_4|\b4\d\d\b", _status(node)):
                continue
            body = node.args[0] if node.args else None
            if isinstance(body, ast.Dict):
                named = any(isinstance(k, ast.Constant) and k.value == "code"
                            for k in body.keys)
            else:
                named = body is not None and any(h in ast.unparse(body)
                                                 for h in CODED_HELPERS)
            if not named:
                unnamed.append(f"{rel}:{node.lineno}")
    assert unnamed == [], unnamed
