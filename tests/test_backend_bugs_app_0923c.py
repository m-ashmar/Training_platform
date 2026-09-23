"""BACKEND_BUGS B-053..B-057: what the app session found wiring the Me screens
(2026-09-23), one test per row.

Each fails on the code as the app found it. The error envelope already filled a
generic `code` (`bad_request`, `not_found`) into every code-less refusal, so the
refusal tests pin the view's OWN code — a generic one is what the app could not tell
from a routing 404 or any other 400.
"""
import json

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
PROFILE = "/api/auth/user/update/"
PICTURE = "/api/auth/user/profile-picture/"
DEVICE_TOKEN = "/api/auth/device-token/"


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


# ------------------------------------------------------------------------ B-053
def test_the_profile_serves_the_phone_number_read_only(make_user, api):
    """EditProfile's "login details" group draws the phone row with a lock; the payload
    did not carry it. A number sent back is ignored, and the rest of the request saved,
    like every other read-only field there."""
    user = make_user("b053", phone_number="+963912345678")

    assert api(user).get(PROFILE).json()["phone_number"] == "+963912345678"

    resp = api(user).post(PROFILE, {"phone_number": "+963900000000", "first_name": "Samer"},
                          content_type=JSON)
    assert resp.status_code == 200, resp.content
    user.refresh_from_db()
    assert user.phone_number == "+963912345678", "the number is changed through support only"
    assert user.first_name == "Samer", "the rest of the request is saved"

    # The column's default is a placeholder (also what an erased account keeps), not a
    # number anybody owns.
    nobody = make_user("b053_none")
    assert nobody.phone_number == "0000000000"
    assert api(nobody).get(PROFILE).json()["phone_number"] is None


# ------------------------------------------------------------------------ B-054
def test_unregistering_a_device_token_refuses_with_its_own_codes(make_user, api):
    """The POST side carried codes since B-025; the DELETE side answered code-less."""
    from users.models import DeviceToken

    user = make_user("b054")
    other = make_user("b054_other")
    DeviceToken.objects.create(user=other, token="tok-b054-other", platform="ios")

    missing = api(user).delete(DEVICE_TOKEN, data=json.dumps({}), content_type=JSON)
    assert (missing.status_code, missing.json()["code"], missing.json()["field"]) == \
        (400, "token_required", "token"), missing.content

    for token in ("tok-b054-never", "tok-b054-other"):
        unknown = api(user).delete(DEVICE_TOKEN, data=json.dumps({"token": token}),
                                   content_type=JSON)
        assert (unknown.status_code, unknown.json()["code"]) == (404, "token_not_found"), \
            (token, unknown.content)
    assert DeviceToken.objects.filter(token="tok-b054-other").exists(), \
        "another account's token is not this account's to remove"

    DeviceToken.objects.create(user=user, token="tok-b054-mine", platform="ios")
    gone = api(user).delete(DEVICE_TOKEN, data=json.dumps({"token": "tok-b054-mine"}),
                            content_type=JSON)
    assert gone.status_code == 200
    assert not DeviceToken.objects.filter(token="tok-b054-mine").exists()


# ------------------------------------------------------------------------ B-055
def test_an_upload_without_the_picture_part_says_so_with_its_own_code(make_user, api):
    """Every other refusal of the view named its code; this one did not."""
    user = make_user("b055")
    resp = api(user).post(PICTURE, {"first_name": "not a picture"})
    assert resp.status_code == 400, resp.content
    body = resp.json()
    assert (body["code"], body["field"]) == ("profile_picture_required", "profile_picture")
    assert body["error"] and body["detail"]


# ------------------------------------------------------------------------ B-056
def test_removing_a_picture_that_is_not_there_is_told_from_a_routing_404(make_user, api):
    user = make_user("b056")
    resp = api(user).delete(PICTURE)
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "profile_picture_not_found", body
    assert body["message"], "the key the app reads today is kept"
    assert body["error"] and body["detail"]


# ------------------------------------------------------------------------ B-057
def test_a_saved_profile_is_answered_in_the_shape_get_answers(make_user, api):
    """The app followed every save with a GET to read back what was stored."""
    user = make_user("b057", phone_number="+963912345678")
    resp = api(user).post(PROFILE, {"first_name": "Samer", "weight": 72.5,
                                    "activity_level": "Sedentary"}, content_type=JSON)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body.pop("message"), "the key the app reads today is kept"

    fetched = api(user).get(PROFILE).json()
    assert body == fetched, "the answer is the saved profile, as GET reads it"
    assert (body["first_name"], body["weight"], body["activity_level"]) == \
        ("Samer", 72.5, "Sedentary")
