"""SIDE_BUGS S-272 (Road to 10 round 4): `POST /api/achievements/check/` re-ran the whole
achievement engine on every call with no limit of its own. It is now limited per account
(`achievement_check`, 6 an hour): the app never calls it, and badges are awarded after
every activity anyway. Past the limit, the platform's 429 `throttled` shape."""
import uuid

import pytest

pytestmark = pytest.mark.django_db

CHECK = "/api/achievements/check/"


@pytest.fixture(autouse=True)
def _fresh_throttles():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def test_the_check_is_limited_per_account(make_user, api):
    person = make_user(f"s272_{uuid.uuid4().hex[:6]}")
    for _ in range(6):
        assert api(person).post(CHECK).status_code == 200
    refused = api(person).post(CHECK)
    assert refused.status_code == 429, refused.content
    body = refused.json()
    assert body["code"] == "throttled" and body["error"]
    assert int(refused["Retry-After"]) > 0

    # Per account: somebody else is not held back by it.
    other = make_user(f"s272o_{uuid.uuid4().hex[:6]}")
    assert api(other).post(CHECK).status_code == 200


def test_the_rate_is_six_an_hour():
    from django.conf import settings

    assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["achievement_check"] == \
        "6/hour"
