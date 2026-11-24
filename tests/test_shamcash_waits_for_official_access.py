"""The ShamCash gateway waits for ShamCash's official access (the owner, 2026-11-24).

"Do not take nothing from the third party." The gateway's API base URL defaulted to a
third party's host, so a token was enough to send the merchant account's reads there.
Now `SHAMCASH_API_URL` has no default and nothing names that host: unset, no payment API
is called — whatever token is set — a status read answers 'unknown' (which closes
nothing), a listing answers nothing, incompletely, hosted mode is off, and the gateway
is not `automatic`: a checkout takes the manual transfer a person confirms when the
merchant account is set, or is refused (`gateway_unavailable`) when it is not. How a
payment's state moves is unchanged.
"""
import pathlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_road10_s213_wallet_plan import _person, _plan, _subscription

pytestmark = pytest.mark.django_db

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def no_outbound_calls(monkeypatch):
    """Every way `requests` could reach the network fails the test."""
    import requests

    calls = []

    def refuse(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError(f"an outbound request was made: {args!r}")

    monkeypatch.setattr(requests, "get", refuse)
    monkeypatch.setattr(requests, "post", refuse)
    monkeypatch.setattr(requests.sessions.Session, "request", refuse)
    return calls


@pytest.fixture
def token_but_no_url():
    """A token and a merchant account, and no base URL: the state of a server whose
    ShamCash variables were filled in before ShamCash's official access arrived."""
    from subscription.settings import gateway_config

    config = gateway_config.GATEWAY_REGISTRY["shamcash"]["config"][gateway_config.GATEWAY_MODE]
    before = dict(config)
    config.update(api_key="a-token", api_url="", merchant_id="WAIT-ACCOUNT",
                  initiate_path="/payments/initiate", webhook_secret="")
    try:
        yield config
    finally:
        config.clear()
        config.update(before)


def test_no_code_or_deploy_file_names_the_third_party():
    hits = []
    for folder in ("subscription", "training_platform", "deploy", "wallet", "policy"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".example", ".md", ".yml", ".sh",
                                                  ".toml", ".json", ".txt"):
                if "shamcash-api" in path.read_text(errors="ignore").lower():
                    hits.append(str(path.relative_to(ROOT)))
    for doc in ("DEPLOY_CHECKLIST.md", "deploy/env.example"):
        if "shamcash-api" in (ROOT / doc).read_text().lower():
            hits.append(doc)
    assert hits == []


def test_the_base_url_has_no_default():
    from subscription.gateways.shamcash import ShamCashGateway

    assert ShamCashGateway({"api_key": "a-token"}).base_url == ""


def test_an_unset_base_url_makes_no_outbound_call(no_outbound_calls, token_but_no_url):
    from subscription.gateways.shamcash import ShamCashGateway

    gateway = ShamCashGateway(dict(token_but_no_url))
    status = gateway.fetch_payment_status(
        "SHAMCASH_1732400000000_abcdef12",
        expected={"amount": Decimal("5000"), "currency": "SYP"})
    assert status["status"] == "unknown"
    assert gateway.list_transactions(timezone.now() - timedelta(hours=1), timezone.now()) \
        == ([], False)
    opened = gateway.initiate_payment({"reference": "SHAMCASH_1_abcdef12", "amount": 5000,
                                       "currency": "SYP"})
    # Hosted mode needs the base URL too: the manual transfer, with its instructions.
    assert opened["payment_url"] is None
    assert opened["instructions"]["account_id"] == "WAIT-ACCOUNT"
    assert gateway.last_read_error is None, "no request, so nothing said about ShamCash"
    assert no_outbound_calls == []


def test_a_token_alone_is_not_an_automatic_gateway(token_but_no_url, api, make_user):
    from subscription.settings import gateway_config

    assert gateway_config.is_gateway_enabled("shamcash") is False
    assert gateway_config.gateway_kind("shamcash") == "reviewed"
    assert gateway_config.can_initiate_payments("shamcash") is True     # the manual step
    token_but_no_url["merchant_id"] = ""
    assert gateway_config.gateway_kind("shamcash") is None
    assert gateway_config.can_initiate_payments("shamcash") is False    # refused
    token_but_no_url.update(merchant_id="WAIT-ACCOUNT", api_url="https://shamcash.test/v1")
    assert gateway_config.gateway_kind("shamcash") == "automatic"

    token_but_no_url["api_url"] = ""
    listed = api(make_user("wait_list")).get("/api/subscription/v1/gateways/",
                                             HTTP_X_APP_CHANNEL="apk").json()
    [shamcash] = listed["gateways"]
    assert (shamcash["kind"], shamcash["available"]) == ("reviewed", True)


def test_the_poller_reads_nothing_and_closes_nothing(no_outbound_calls, token_but_no_url,
                                                     make_user):
    from subscription.models import GatewayHealth, Payment
    from subscription.tasks import poll_open_payments

    user = _person(make_user, "wait_poll")
    sub = _subscription(user, _plan("wait poll"))
    payment = Payment.objects.create(subscription=sub, amount=Decimal("2500.00"),
                                     currency="SYP", payment_method="shamcash",
                                     status="pending",
                                     gateway_transaction_reference="SHAMCASH_1732400000000_feedbeef")
    Payment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(minutes=5))
    poll_open_payments()
    payment.refresh_from_db()
    assert payment.status == "pending"
    assert no_outbound_calls == []
    assert not GatewayHealth.objects.filter(gateway="shamcash",
                                            consecutive_failures__gt=0).exists()
