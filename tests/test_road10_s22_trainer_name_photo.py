"""Road to 10 S2.2: a trainer has a name and a photo.

The profile already took names and an own picture for every role; what failed: a name of
spaces read as given (`onboarding_missing` did not name it) while the directory leaves
such a trainer out, so the app could not know to ask; and the console had no way to take
a reported picture down but to edit each account. These fail on the code before it."""
import pytest

pytestmark = pytest.mark.django_db


def test_a_trainer_whose_name_is_spaces_is_asked_for_it(make_user, api):
    trainer = make_user("s22_spaces", user_type="trainer", first_name="   ",
                        last_name="Haddad")
    missing = api(trainer).get("/api/auth/user/update/").json()["onboarding_missing"]
    assert "first_name" in missing and "last_name" not in missing


def test_a_trainer_uploads_an_own_photo(make_user, api):
    from tests.test_design_v2 import _image

    trainer = make_user("s22_photo", user_type="trainer", first_name="Rami")
    resp = api(trainer).post("/api/auth/user/profile-picture/",
                             {"profile_picture": _image(400, 400, name="me")})
    assert resp.status_code == 200, resp.content
    assert resp.json()["profile_picture_url"]


def test_an_operator_takes_a_reported_picture_down(make_user, client):
    from users.models import CustomUser

    trainer = make_user("s22_down", user_type="trainer", first_name="Rami")
    CustomUser.objects.filter(pk=trainer.pk).update(profile_picture="profile_pics/x.png")
    client.force_login(make_user("s22_operator", user_type="admin"))
    resp = client.post("/dj-admin/users/customuser/",
                       {"action": "remove_profile_pictures",
                        "_selected_action": [trainer.pk]})
    assert resp.status_code in (200, 302), resp.content[:300]
    trainer.refresh_from_db()
    assert not trainer.profile_picture
