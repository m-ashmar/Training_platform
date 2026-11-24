"""A trainer is limited exactly as a client is: 1500 requests an hour (the owner,
2026-11-24; SIDE_BUGS S-284).

`RateLimitMiddleware` buckets a signed-in caller by user id and limits the bucket by the
account's type. A client's was 1500 (the owner's number of 2026-09-19) and a trainer's
1000, though a trainer uses the client's routes too (the community, Road to 10 S3.8) on
top of the coach's own. Both now hold 1500, and the request after the 1500th in the
window is refused with the contract's `rate_limited` answer. (`tests/test_backend_bugs.py`
pins the client's number on its own.)
"""
import pytest

pytestmark = pytest.mark.django_db

OWNERS_NUMBER = 1500
ROUTE = "/api/v1/config/capabilities"


def test_a_trainer_and_a_client_have_the_same_allowance(settings):
    from training_platform.middleware import RateLimitMiddleware

    settings.DEBUG = False
    limits = RateLimitMiddleware(lambda request: None).rate_limits
    assert limits["trainer"] == limits["client"] == {"requests": OWNERS_NUMBER,
                                                     "window": 3600}


@pytest.mark.parametrize("user_type", ["trainer", "client"])
def test_the_request_after_the_allowance_is_refused(settings, make_user, api, user_type):
    """The bucket is filled to one below the allowance instead of sending 1500 requests:
    the 1500th passes and the 1501st is refused, for a trainer as for a client."""
    from training_platform.cache import ratelimit_cache

    settings.DEBUG = False            # the middleware reads it when the client loads it
    person = make_user(f"rl1500_{user_type}", user_type=user_type)
    caller = api(person)
    ratelimit_cache().set(f"rate_limit:user_{person.pk}", OWNERS_NUMBER - 1, 3600)

    last = caller.get(ROUTE)
    assert last.status_code == 200, last.content
    over = caller.get(ROUTE)
    assert over.status_code == 429, over.content
    assert over.json()["code"] == "rate_limited" and int(over["Retry-After"]) > 0
