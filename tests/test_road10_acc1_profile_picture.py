"""The fix run's ACC-1, S-200 (2026-11-24): a picture file sent to `POST trainer/profile/`
or `POST client/profile/` is refused, as `user/update/` refuses it (the owner,
2026-09-20, BACKEND_BUGS B-026: "refused, not dropped"). Both profile routes dropped it
and answered 200 — their serializers read the picture, never write it — so the app
showed the old picture as saved. The refusals fail on the code before.
"""
import io
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

pytestmark = pytest.mark.django_db


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, format="JPEG")
    return SimpleUploadedFile("me.jpg", buf.getvalue(), content_type="image/jpeg")


@pytest.mark.parametrize("role, route, field", [
    ("trainer", "/api/auth/trainer/profile/", "trainer_bio"),
    ("client", "/api/auth/client/profile/", "first_name"),
])
def test_a_picture_sent_to_a_profile_route_is_refused_and_nothing_is_saved(
        make_user, api, role, route, field):
    person = make_user(f"s200_{role}_{uuid.uuid4().hex[:6]}", user_type=role)
    before = getattr(person, field)

    resp = api(person).post(route, {field: "Changed", "profile_picture": _jpeg()})
    assert resp.status_code == 400, resp.content
    assert (resp.json()["code"], resp.json()["field"]) == ("profile_picture_read_only",
                                                           "profile_picture")
    person.refresh_from_db()
    assert getattr(person, field) == before and not person.profile_picture


@pytest.mark.parametrize("role, route, field", [
    ("trainer", "/api/auth/trainer/profile/", "trainer_bio"),
    ("client", "/api/auth/client/profile/", "first_name"),
])
def test_the_picture_url_echoed_as_text_is_not_refused(make_user, api, role, route, field):
    person = make_user(f"s200e_{role}_{uuid.uuid4().hex[:6]}", user_type=role)
    resp = api(person).post(route, {field: "Changed",
                                    "profile_picture": "https://cdn.example/me.jpg"})
    assert resp.status_code == 200, resp.content
    person.refresh_from_db()
    assert getattr(person, field) == "Changed"
