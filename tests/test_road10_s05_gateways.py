"""Road to 10 S0.5 (the owner's D5, 2026-11-22): the gateway list is honest.

The app sells a plan only when a way to pay exists and words the promise by its kind.
`GET /api/subscription/v1/gateways/` listed a gateway only when its API token was set,
and said nothing about how a payment is confirmed: a transfer deployment (the merchant
account id alone, confirmed by a person in the console) was hidden although it takes
payments, and nothing said "no way to pay" except an empty list. Each gateway now says
`available`, `kind` (`automatic` or `reviewed`) and, for `reviewed`, `review_hours`; the
answer says `can_pay` and why not. It depends on the build and on nothing else. Every
assertion on the new keys fails on the code before this change.
"""
import pytest

pytestmark = pytest.mark.django_db

URL = "/api/subscription/v1/gateways/"


@pytest.fixture
def shamcash_config():
    from subscription.settings import gateway_config

    config = gateway_config.GATEWAY_REGISTRY["shamcash"]["config"][
        gateway_config.GATEWAY_MODE]
    before = dict(config)
    config.update(api_key="", merchant_id="", webhook_secret="", initiate_path="")
    try:
        yield config
    finally:
        config.clear()
        config.update(before)


def _answer(api, user, channel="apk"):
    resp = api(user).get(URL, HTTP_X_APP_CHANNEL=channel)
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_no_way_to_pay_is_said_before_any_checkout(make_user, api, shamcash_config):
    body = _answer(api, make_user("s05_none"))
    assert (body["can_pay"], body["reason"]) == (False, "no_gateway")
    [shamcash] = body["gateways"]
    assert (shamcash["name"], shamcash["available"], shamcash["enabled"]) == \
        ("shamcash", False, False)
    assert shamcash["kind"] is None and shamcash["review_hours"] is None


def test_a_gateway_the_server_reads_is_automatic(make_user, api, shamcash_config):
    shamcash_config.update(api_key="tok", merchant_id="ACC-1")
    body = _answer(api, make_user("s05_auto"))
    [shamcash] = body["gateways"]
    assert (body["can_pay"], body["reason"]) == (True, None)
    assert (shamcash["available"], shamcash["kind"], shamcash["review_hours"]) == \
        (True, "automatic", None)


def test_a_transfer_a_person_confirms_is_reviewed_with_its_hours(make_user, api, settings,
                                                                 shamcash_config):
    settings.PAYMENT_REVIEW_HOURS = 12
    shamcash_config.update(merchant_id="ACC-1")
    body = _answer(api, make_user("s05_reviewed"))
    [shamcash] = body["gateways"]
    assert body["can_pay"] is True
    assert (shamcash["available"], shamcash["kind"], shamcash["review_hours"]) == \
        (True, "reviewed", 12)


@pytest.mark.parametrize("channel", ["ios", "play"])
def test_a_build_that_may_not_sell_is_told_so_not_shown_a_gateway(make_user, api, channel,
                                                                  shamcash_config):
    shamcash_config.update(api_key="tok", merchant_id="ACC-1")
    body = _answer(api, make_user(f"s05_{channel}"), channel=channel)
    assert body["gateways"] == []
    assert (body["can_pay"], body["reason"]) == (False, "not_available_on_channel")
