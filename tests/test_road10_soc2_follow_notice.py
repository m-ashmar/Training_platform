"""The fix run's SOC-2, S-285 (2026-11-24): a follow made with `POST /api/social/follows/`
tells the person followed (`user_followed`), as one made with `follow_user/` does. It
saved the follow and sent nothing. The first test fails on the code before.
"""
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _people(make_user):
    return (make_user(f"s285_a_{uuid.uuid4().hex[:6]}", email_verified=True),
            make_user(f"s285_b_{uuid.uuid4().hex[:6]}", email_verified=True))


def _told(person, follower):
    from notifications.models import Notification

    return Notification.objects.filter(recipient=person, actor=follower,
                                       event_type="user_followed").count()


def test_a_follow_made_on_follows_tells_the_person_followed(
        make_user, api, django_capture_on_commit_callbacks):
    fan, star = _people(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        made = api(fan).post("/api/social/follows/", {"following_id": star.pk},
                             content_type=JSON)
    assert made.status_code == 201, made.content
    assert _told(star, fan) == 1


def test_follow_user_still_tells_them_once(make_user, api,
                                          django_capture_on_commit_callbacks):
    fan, star = _people(make_user)
    with django_capture_on_commit_callbacks(execute=True):
        assert api(fan).post("/api/social/follows/follow_user/", {"user_id": star.pk},
                             content_type=JSON).status_code == 201
        again = api(fan).post("/api/social/follows/follow_user/", {"user_id": star.pk},
                              content_type=JSON)
    assert again.status_code == 200          # already following: nothing new to tell
    assert _told(star, fan) == 1
