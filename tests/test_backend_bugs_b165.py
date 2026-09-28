"""BACKEND_BUGS B-165: an invitation row says whether the trainer is verified, and shows
their picture.

The Invitations artboard shows «مدرب موثّق» / «غير موثّق بعد» and the trainer's picture
on each card, and a row carried neither: the app fetched the trainer's card once per
invitation. The owner (2026-09-28, privacy-first): add `trainer_is_verified` and
`trainer_profile_picture` to the invitation rows, and NO gender.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db

AUTH = "/api/auth"
JSON = "application/json"


def _invite(api, trainer, client):
    sent = api(trainer).post(f"{AUTH}/trainer/assign-client/", {"client_id": client.pk},
                             content_type=JSON)
    assert sent.status_code == 200, sent.content


def test_b165_an_invitation_row_carries_the_trainers_verification_and_picture(make_user, api):
    client = make_user("b165_c")
    # Verified as the verification service writes it; a picture on file.
    verified = make_user("b165_verified", user_type="trainer", trainer_is_verified=True,
                         profile_picture="profile_pictures/b165_verified.jpg")
    plain = make_user("b165_plain", user_type="trainer")
    _invite(api, verified, client)
    _invite(api, plain, client)

    rows = {row["trainer_id"]: row for row in
            api(client).get(f"{AUTH}/client/trainer-invites/").json()["invitations"]}
    assert rows[verified.pk]["trainer_is_verified"] is True
    assert rows[verified.pk]["trainer_profile_picture"], rows[verified.pk]
    assert "b165_verified.jpg" in rows[verified.pk]["trainer_profile_picture"]
    assert rows[plain.pk]["trainer_is_verified"] is False
    assert rows[plain.pk]["trainer_profile_picture"] is None
    # The owner: no gender on any payload.
    assert not any("gender" in key for row in rows.values() for key in row)
