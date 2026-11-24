"""The fix run's ACC-1, S-246 (2026-11-24): every profile picture in `users` is a URL
from `training_platform.media_storage.media_url`, the one function that turns a stored
file into a URL — signed where the backend signs, absolute with the request. The
profile serializers and nine views built it by hand from `FieldFile.url`, and
`GET trainer/profile/` passed them no request, so on the filesystem backend the trainer
read a RELATIVE picture URL. The first three tests fail on the code before.
"""
import io
import re
import uuid
from pathlib import Path

import pytest
from django.core.files.base import ContentFile
from PIL import Image

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)


def _with_picture(make_user, role):
    person = make_user(f"s246_{role}_{uuid.uuid4().hex[:6]}", user_type=role)
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 200)).save(buf, format="JPEG")
    person.profile_picture.save("me.jpg", ContentFile(buf.getvalue()), save=True)
    return person


@pytest.mark.parametrize("role, route", [("trainer", "/api/auth/trainer/profile/"),
                                         ("client", "/api/auth/client/profile/")])
def test_the_own_profile_carries_an_absolute_picture_url(make_user, api, role, route):
    from training_platform.media_storage import media_url

    person = _with_picture(make_user, role)
    url = api(person).get(route).json()["profile_picture"]
    assert url.startswith("http://testserver/"), url
    assert url == "http://testserver" + media_url(person.profile_picture)


def test_no_picture_url_in_users_is_built_by_hand():
    """The one-function rule, held in the source: nothing in `users` reads
    `profile_picture.url` (or a picture's `.url`) to hand out."""
    offenders = []
    for path in sorted((ROOT / "users").glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"(profile_picture|picture)\.url\b", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], offenders


def test_no_picture_is_still_null(make_user, api):
    person = make_user(f"s246_none_{uuid.uuid4().hex[:6]}", user_type="trainer")
    assert api(person).get("/api/auth/trainer/profile/").json()["profile_picture"] is None
