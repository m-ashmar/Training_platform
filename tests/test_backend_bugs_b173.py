"""BACKEND_BUGS B-173: saving a trainer's profile answers the saved profile.

`user/update/` echoes what it stored (B-057), so the app needs no second request;
`trainer/profile/` POST answered `{message}` alone and the app re-read GET after every
save. It now answers the saved profile in GET's shape, plus `message` (additive).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"


def test_b173_trainer_profile_post_echoes_the_saved_profile(make_user, api):
    trainer = make_user("b173_t", user_type="trainer")
    url = "/api/auth/trainer/profile/"

    saved = api(trainer).post(url, {"trainer_bio": "Coach, 10y."}, content_type=JSON)
    assert saved.status_code == 200, saved.content
    body = saved.json()
    assert body["trainer_bio"] == "Coach, 10y."
    assert body["message"]
    read = api(trainer).get(url).json()
    assert set(read) <= set(body), set(read) - set(body)
    assert {key: body[key] for key in read} == read, "the echo is what GET reads back"
