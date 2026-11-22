"""Road to 10 S2.8 (the owner's D14): change the password while signed in, ending the
other sessions. The route did not exist; every test here fails on the code before it."""
import pytest

pytestmark = pytest.mark.django_db

URL = "/api/auth/password/change/"
JSON = "application/json"
OLD, NEW = "Xx!23456", "Brand-new-pass-91"


def _sign_in(api, user):
    """A real sign-in: a refresh token the server has on record (OutstandingToken)."""
    resp = api().post("/api/auth/token/", {"email": user.email, "password": OLD},
                      content_type=JSON)
    assert resp.status_code == 200, resp.content
    return resp.json()["refresh"]


@pytest.fixture
def person(make_user):
    return make_user("s28_person", email_verified=True)


def test_the_password_changes_and_every_other_session_ends(api, person):
    other_device = _sign_in(api, person)

    resp = api(person).post(URL, {"current_password": OLD, "new_password": NEW},
                            content_type=JSON)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["access"] and body["refresh"]

    person.refresh_from_db()
    assert person.check_password(NEW) and not person.check_password(OLD)
    # The other device's refresh token is dead; this device's new one works.
    dead = api().post("/api/auth/token/refresh/", {"refresh": other_device},
                      content_type=JSON)
    assert dead.status_code == 401, dead.content
    alive = api().post("/api/auth/token/refresh/", {"refresh": body["refresh"]},
                       content_type=JSON)
    assert alive.status_code == 200, alive.content


def test_a_wrong_current_password_changes_nothing(api, person):
    resp = api(person).post(URL, {"current_password": "not-it", "new_password": NEW},
                            content_type=JSON)
    assert resp.status_code == 400
    assert (resp.json()["code"], resp.json()["field"]) == ("current_password_wrong",
                                                            "current_password")
    person.refresh_from_db()
    assert person.check_password(OLD)


def test_the_new_password_follows_the_sign_up_rules_and_must_differ(api, person):
    weak = api(person).post(URL, {"current_password": OLD, "new_password": "123"},
                            content_type=JSON)
    assert weak.status_code == 400
    codes = {e["code"] for e in weak.json()["field_errors"]["new_password"]}
    assert "password_too_short" in codes

    same = api(person).post(URL, {"current_password": OLD, "new_password": OLD},
                            content_type=JSON)
    assert (same.status_code, same.json()["code"]) == (400, "password_unchanged")


def test_guessing_the_current_password_is_stopped(api, person):
    from training_platform.cache import ratelimit_cache

    ratelimit_cache().clear()
    try:
        for _ in range(5):
            api(person).post(URL, {"current_password": "guess", "new_password": NEW},
                             content_type=JSON)
        stopped = api(person).post(URL, {"current_password": OLD, "new_password": NEW},
                                   content_type=JSON)
        assert stopped.status_code == 429
        assert stopped.json()["code"] == "throttled"
        assert int(stopped["Retry-After"]) > 0
    finally:
        ratelimit_cache().clear()


def test_signed_out_is_refused(api):
    assert api().post(URL, {"current_password": OLD, "new_password": NEW},
                      content_type=JSON).status_code == 401
