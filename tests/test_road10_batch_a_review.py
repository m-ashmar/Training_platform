"""Road to 10 batch A, the adversarial review (2026-11-23): one test per confirmed finding,
each failing on the code before its fix.

  S2.8  a new password typed with spaces around it could never sign in again
  S2.8  guesses sent at once all passed the limit of five (the race itself is in
        tests/test_zz_road10_s28_guessing.py; here, that a right password costs no slot)
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
CHANGE = "/api/auth/password/change/"
OLD, NEW = "Xx!23456", "Brand-new-pass-91"


def _name(tag):
    return f"r10a_{tag}_{uuid.uuid4().hex[:6]}"


# ------------------------------------------------------------------ S2.8, spaces
def test_s28_a_password_typed_with_spaces_signs_in_as_typed(make_user, api):
    """Every sign-in route trims the password it is given (DRF's default); the change
    route stored the spaces. The person was then locked out of signing in with a
    password: the trimmed string was not the stored one, and the untrimmed one was
    trimmed on the way in."""
    person = make_user(_name("spaces"), email_verified=True)
    typed = f"  {NEW} "
    changed = api(person).post(CHANGE, {"current_password": f" {OLD}  ",
                                        "new_password": typed}, content_type=JSON)
    assert changed.status_code == 200, changed.content

    for route in ("/api/auth/token/", "/api/auth/login/"):
        signed_in = api().post(route, {"email": person.email, "password": typed},
                               content_type=JSON)
        assert signed_in.status_code == 200, (route, signed_in.content)
    person.refresh_from_db()
    assert person.check_password(NEW)


# ------------------------------------------------------------------ S2.8, the limit
def test_s28_a_right_current_password_does_not_use_up_the_limit(make_user, api):
    """Each attempt now takes its slot before the check; one whose current password was
    right gives it back, so only wrong guesses count."""
    from training_platform.cache import ratelimit_cache

    ratelimit_cache().clear()
    person = make_user(_name("limit"), email_verified=True)
    try:
        for _ in range(4):
            assert api(person).post(CHANGE, {"current_password": "guess",
                                             "new_password": NEW},
                                    content_type=JSON).json()["code"] == \
                "current_password_wrong"
        for _ in range(3):
            same = api(person).post(CHANGE, {"current_password": OLD, "new_password": OLD},
                                    content_type=JSON)
            assert same.json()["code"] == "password_unchanged"
        fifth = api(person).post(CHANGE, {"current_password": "guess", "new_password": NEW},
                                 content_type=JSON)
        assert fifth.json()["code"] == "current_password_wrong"
        sixth = api(person).post(CHANGE, {"current_password": OLD, "new_password": NEW},
                                 content_type=JSON)
        assert (sixth.status_code, sixth.json()["code"]) == (429, "throttled")
    finally:
        ratelimit_cache().clear()
