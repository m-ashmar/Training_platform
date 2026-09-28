"""BACKEND_BUGS B-172: the sign-up the app sends, which carries no `username`.

The Django 5.2 upgrade (allauth 65, dj-rest-auth 7.2) made `username` REQUIRED on
`POST /api/auth/register/`: dj-rest-auth 7.2 reads a field missing from allauth's
`ACCOUNT_SIGNUP_FIELDS` as required. Every register test sent a username, so the gate
never saw the app's body, and every sign-up from the app answered 400. These tests post
exactly what the app posts.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
REGISTER = "/api/auth/register/"


def _apps_body(email):
    """The keys the v2 app sends (yalla_gym api_start_repository): no username."""
    return {"user_type": "client", "email": email, "phone_number": "+963999999999",
            "password1": "Xx!23456long", "password2": "Xx!23456long",
            "date_of_birth": "1995-06-15", "preferred_language": "ar"}


def test_b172_the_apps_register_body_without_a_username_signs_up(api):
    from users.models import CustomUser

    email = f"signup{uuid.uuid4().hex[:8]}@gate.test"
    resp = api().post(REGISTER, _apps_body(email), content_type=JSON)
    assert resp.status_code == 201, resp.content
    user = CustomUser.objects.get(email=email)
    assert user.username, "a name is derived from the e-mail when none is sent"


def test_b172_two_people_with_the_same_local_part_both_sign_up(api):
    from users.models import CustomUser

    local = f"same{uuid.uuid4().hex[:6]}"
    for domain in ("one.test", "two.test"):
        resp = api().post(REGISTER, _apps_body(f"{local}@{domain}"), content_type=JSON)
        assert resp.status_code == 201, resp.content
    names = set(CustomUser.objects.filter(email__startswith=f"{local}@")
                .values_list("username", flat=True))
    assert len(names) == 2, names


@pytest.fixture
def banned_list(db):
    """A list of our own: the seeded one is cached per process (tests/test_ugc_filter.py)."""
    from moderation import ugc_filter
    from moderation.models import BannedTerm

    BannedTerm.objects.all().delete()
    ugc_filter.invalidate()
    yield BannedTerm
    ugc_filter.invalidate()


def test_b172_a_banned_term_in_the_local_part_never_becomes_the_public_username(api,
                                                                                banned_list):
    from moderation import ugc_filter
    from users.models import CustomUser

    banned_list.objects.create(term="zorblax", whole_word=False, language="en",
                               category="obscenity")
    ugc_filter.invalidate()
    email = f"zorblax{uuid.uuid4().hex[:6]}@gate.test"
    resp = api().post(REGISTER, _apps_body(email), content_type=JSON)
    assert resp.status_code == 201, resp.content      # never refused: nobody typed it
    username = CustomUser.objects.get(email=email).username
    assert ugc_filter.find_banned(username) is None, username
    assert username.startswith("user"), username


def test_b172_a_reserved_platform_name_is_never_derived(api):
    from users.models import CustomUser
    from wallet.utils import is_reserved_username

    email = f"platform_escrow_{uuid.uuid4().hex[:4]}@gate.test"
    resp = api().post(REGISTER, _apps_body(email), content_type=JSON)
    assert resp.status_code == 201, resp.content
    username = CustomUser.objects.get(email=email).username
    assert not is_reserved_username(username), username


def test_b172_a_sent_username_is_still_checked(api):
    """The explicit path keeps its rules: a reserved platform name is `username_taken`."""
    body = _apps_body(f"reserved{uuid.uuid4().hex[:8]}@gate.test")
    body["username"] = "platform_escrow"
    resp = api().post(REGISTER, body, content_type=JSON)
    assert resp.status_code == 400, resp.content
    assert resp.json()["field_errors"]["username"][0]["code"] == "username_taken"
