"""Around the checkout: the rest of the payments spec's sections 5 and 6 (2026-09-27).

1. Returning to the app (section 5): Android App Links and iOS Universal Links on the
   hub domain, from the environment, 404 until configured, never a guessed id — and the
   link carries no secret and no entitlement.
2. "Save the address, not the account" (section 6): a customer's public ShamCash
   address, typed twice, encrypted, removable, in the privacy registry — and used as the
   SECOND matching signal only: a transfer with the right amount and a missing or
   mistyped reference, from that address, holds the customer's checkout for an
   operator with the match as evidence. It never confirms a payment.
3. "ShamCash is not responding, your payment is safe, we will keep checking" (section
   6): the pollers record whether the merchant account could be read, every payment
   read answers `gateway_state`, nothing expires because the gateway is down, and the
   console shows it.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from tests.test_backend_bugs_app_0923b import _plan
from tests.test_fx_subs import _checkout, _person, _post, _subscribe, shamcash  # noqa: F401
from tests.test_payment_review import _account, _open_checkout, _poll, _sweep, _txn

pytestmark = pytest.mark.django_db

D = Decimal
ADDRESS = "0a3f91c27be455d0"
PAYER = "/api/subscription/v1/payer-address/"
ASSETLINKS = "/.well-known/assetlinks.json"
AASA = "/.well-known/apple-app-site-association"
FINGERPRINT = "14:6D:E9:83:C5:73:06:50:D8:EE:B9:95:2F:34:FC:64:16:A0:83:42:E6:1D:BE:A8:8A:04:96:B2:3F:CF:44:E5"


def _put(c, address, confirm=None, **extra):
    return c.put(PAYER, data=json.dumps({"address": address,
                                        "address_confirm": address if confirm is None
                                        else confirm}),
                 content_type="application/json", **extra)


def _saved(api, user, address=ADDRESS):
    assert _put(api(user), address).status_code == 200


def _events(payment):
    from subscription.models import PaymentEvent

    return list(PaymentEvent.objects.filter(payment=payment).order_by("created_at", "id"))


# =============================================================== 1. back to the app
@pytest.fixture
def app_links(settings):
    settings.APP_LINKS_ANDROID_PACKAGES = "com.yallagym.app"
    # One with colons, one without: both are the SHA-256 of a signing certificate.
    settings.APP_LINKS_ANDROID_SHA256_FINGERPRINTS = (
        f"{FINGERPRINT}, {'ab' * 32}")
    settings.APP_LINKS_IOS_TEAM_ID = "ABCDE12345"
    settings.APP_LINKS_IOS_BUNDLE_IDS = "com.yallagym.app"
    settings.APP_LINKS_PATHS = "/app/*"
    return settings


def test_the_association_files_are_404_with_a_code_until_configured(api, settings):
    settings.APP_LINKS_ANDROID_PACKAGES = ""
    settings.APP_LINKS_ANDROID_SHA256_FINGERPRINTS = ""
    settings.APP_LINKS_IOS_TEAM_ID = ""
    settings.APP_LINKS_IOS_BUNDLE_IDS = ""
    settings.APP_LINKS_PATHS = ""
    for path in (ASSETLINKS, AASA):
        resp = api().get(path)
        assert resp.status_code == 404, path
        assert resp["Content-Type"].startswith("application/json")
        assert resp.json()["code"] == "app_links_not_configured"

    # Half a configuration is not a configuration: a package with no key, a team with no
    # path. Nothing is filled in by guess.
    settings.APP_LINKS_ANDROID_PACKAGES = "com.yallagym.app"
    settings.APP_LINKS_IOS_TEAM_ID = "ABCDE12345"
    settings.APP_LINKS_IOS_BUNDLE_IDS = "com.yallagym.app"
    assert api().get(ASSETLINKS).status_code == 404
    assert api().get(AASA).status_code == 404


def test_assetlinks_names_the_package_and_every_signing_key_from_settings(api, app_links):
    resp = api().get(ASSETLINKS)          # anonymous: Google's verifier has no account
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/json"
    assert "public" in resp["Cache-Control"] and "max-age=3600" in resp["Cache-Control"]
    assert resp.json() == [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {"namespace": "android_app", "package_name": "com.yallagym.app",
                   "sha256_cert_fingerprints": [
                       FINGERPRINT, ":".join(["AB"] * 32)]}}]


def test_apple_app_site_association_routes_the_hub_paths_to_the_app(api, app_links):
    resp = api().get(AASA)
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/json"
    assert "max-age=3600" in resp["Cache-Control"]
    details = resp.json()["applinks"]["details"]
    assert resp.json()["applinks"]["apps"] == []
    assert details[0] == {"appIDs": ["ABCDE12345.com.yallagym.app"],
                          "components": [{"/": "/app/*"}]}
    assert details[1] == {"appID": "ABCDE12345.com.yallagym.app", "paths": ["/app/*"]}


def test_the_association_is_the_same_for_every_build_and_every_caller(
        api, make_user, app_links):
    anonymous = api().get(AASA).content
    for channel in ("apk", "play", "ios", "web"):
        assert api(make_user(f"al_{channel}")).get(
            AASA, HTTP_X_APP_CHANNEL=channel).content == anonymous
    assert api().get(ASSETLINKS, HTTP_X_APP_CHANNEL="ios").content == \
        api().get(ASSETLINKS).content


def test_a_malformed_id_is_left_out_never_passed_through(api, settings):
    settings.APP_LINKS_ANDROID_PACKAGES = "com.yallagym.app"
    settings.APP_LINKS_ANDROID_SHA256_FINGERPRINTS = "not-a-fingerprint"
    settings.APP_LINKS_IOS_TEAM_ID = "abc"                     # not ten characters
    settings.APP_LINKS_IOS_BUNDLE_IDS = "com.yallagym.app"
    settings.APP_LINKS_PATHS = "/app/*"
    assert api().get(ASSETLINKS).json()["code"] == "app_links_not_configured"
    assert api().get(AASA).json()["code"] == "app_links_not_configured"


def test_the_contract_says_the_link_carries_no_secret_and_no_entitlement():
    from pathlib import Path

    from releases import app_links

    for doc in (app_links.__doc__, app_links.AndroidAssetLinksView.__doc__,
                app_links.AppleAppSiteAssociationView.__doc__):
        assert "no secret" in doc.lower() and "no entitlement" in doc.lower()
    contract = (Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text()
    for path in (ASSETLINKS, AASA, "app_links_not_configured"):
        assert path in contract, path


# ======================================================== 2. the saved payer address
def test_the_payer_address_is_typed_twice_and_can_be_changed_and_removed(make_user, api):
    user = make_user("pa_owner")
    c = api(user)
    assert c.get(PAYER).json() == {"address": None, "saved_at": None, "updated_at": None}

    refusals = [(_put(c, ""), "payer_address_required"),
                (_put(c, "123456"), "payer_address_invalid"),   # a PIN is never an address
                (_put(c, ADDRESS, "0a3f91c27be455d1"), "payer_address_mismatch")]
    for resp, code in refusals:
        assert (resp.status_code, resp.json()["code"]) == (400, code)

    saved = _put(c, "0A3F 91C2-7BE4 55D0", "0a3f91c27be455d0")   # spaces, dashes, case
    assert saved.status_code == 200
    assert saved.json()["address"] == "0a3f 91c2 7be4 55d0"
    assert c.get(PAYER).json()["address"] == "0a3f 91c2 7be4 55d0"

    assert _put(c, "11112222333344445555").json()["address"] == "1111 2222 3333 4444 5555"
    from subscription.models import PayerAddress
    assert PayerAddress.objects.filter(user=user).count() == 1, "one per account"

    assert c.delete(PAYER).status_code == 204
    assert c.get(PAYER).json()["address"] is None
    assert c.delete(PAYER).status_code == 204, "forgetting nothing is the same answer"

    assert api().get(PAYER).status_code == 401


def test_the_payer_address_is_the_callers_own_record_on_every_build(make_user, api):
    user = make_user("pa_builds")
    for channel in ("play", "ios", "web", "apk"):
        resp = _put(api(user), ADDRESS, HTTP_X_APP_CHANNEL=channel)
        assert resp.status_code == 200, (channel, resp.content)


def test_the_payer_address_is_encrypted_exported_and_erased(make_user, api):
    from django.db import connection

    from subscription.models import PayerAddress
    from training_platform.privacy.registry import erase_user_data, export_user_data

    user = make_user("pa_privacy")
    _saved(api, user)
    with connection.cursor() as cur:
        cur.execute("SELECT address, address_digest FROM subscription_payeraddress "
                    "WHERE user_id = %s", [user.pk])
        stored, digest = cur.fetchone()
    assert ADDRESS not in stored and ADDRESS not in digest

    section = export_user_data(user)["sections"]["payer_address"]
    assert section[0]["address"] == ADDRESS
    assert "address_digest" not in section[0]

    assert erase_user_data(user)["payer_address"]["action"] == "delete"
    assert not PayerAddress.objects.filter(user=user).exists()


def test_the_right_amount_from_a_saved_address_holds_that_customers_checkout(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """Two open checkouts fit the amount. Without the second signal the transfer is
    queued alone (`several_candidates`); from B's saved address it holds B's checkout
    for review, with the match and the other candidate as evidence — and completes
    nothing."""
    from subscription.models import Payment, Subscription, TransferReview

    plan = _plan("hs1", "7777.00")
    _ca, _sa, first = _open_checkout(api, make_user, "hs_a", plan)
    _cb, sub_b, second = _open_checkout(api, make_user, "hs_b", plan)
    _saved(api, sub_b.user)
    _account(monkeypatch, [_txn("tx-hs1", "7777.00", note="for my plan",
                                sender="0A3F-91C2-7BE4-55D0")])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["held"] == 1

    review = TransferReview.objects.get(transaction_id="tx-hs1")
    assert (review.reason, review.payment_id) == ("reference_missing", second.id)
    assert review.sender_match_payment_ids == [str(second.id)]
    assert review.candidate_payment_ids == [str(first.id)], "the other fit stays in view"
    second.refresh_from_db()
    first.refresh_from_db()
    assert (second.status, first.status) == ("needs_review", "pending")
    held = _events(second)[-1]
    assert (held.to_status, held.evidence["sender_address_matched"]) == ("needs_review", True)
    assert not Payment.objects.filter(status="completed").exists(), "never confirmed"
    assert not Subscription.objects.get(pk=sub_b.pk).is_active

    status = api(sub_b.user).get(
        f"/api/subscription/v1/payments/{second.id}/status/").json()
    assert status["review"] == {"reason": "reference_missing",
                                "held_at": status["review"]["held_at"]}
    assert ADDRESS not in json.dumps(status), "the transfer's details stay in the console"


def test_a_mistyped_reference_from_a_saved_address_finds_the_closed_checkout(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """The customer's checkout expired; their transfer quotes a reference one letter
    off and comes from their saved address. It is still their money: the checkout is
    held, not left for an operator to find in a queue of strangers."""
    from subscription.models import TransferReview
    from subscription.services.payment_service import By, PaymentService

    plan = _plan("hs2", "5000.00")
    c, sub, payment = _open_checkout(api, make_user, "hs_late", plan)
    PaymentService.expire_payment(payment.id, "test", by=By.POLLER)
    _saved(api, sub.user)
    ref = payment.gateway_transaction_reference
    typo = ref[:-1] + ("0" if ref[-1] != "0" else "1")
    _account(monkeypatch, [_txn("tx-hs2", "5000.00", note=f"yalla {typo}", sender=ADDRESS)])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["held"] == 1

    payment.refresh_from_db()
    review = TransferReview.objects.get(transaction_id="tx-hs2")
    assert payment.status == "needs_review"
    assert (review.payment_id, review.sender_match_payment_ids) == \
        (payment.id, [str(payment.id)])


def test_an_address_saved_by_two_accounts_points_at_nobody(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment, TransferReview

    plan = _plan("hs3", "6666.00")
    _ca, sub_a, first = _open_checkout(api, make_user, "hs_twin_a", plan)
    _cb, sub_b, second = _open_checkout(api, make_user, "hs_twin_b", plan)
    _saved(api, sub_a.user)
    _saved(api, sub_b.user)
    _account(monkeypatch, [_txn("tx-hs3", "6666.00", sender=ADDRESS)])

    with django_capture_on_commit_callbacks(execute=True):
        assert _sweep()["queued"] == 1

    review = TransferReview.objects.get(transaction_id="tx-hs3")
    assert (review.reason, review.payment_id) == ("several_candidates", None)
    assert set(review.sender_match_payment_ids) == {str(first.id), str(second.id)}
    assert set(Payment.objects.filter(pk__in=[first.pk, second.pk])
               .values_list("status", flat=True)) == {"pending"}


def test_the_address_alone_with_another_amount_holds_nothing(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import TransferReview

    plan = _plan("hs4", "4000.00")
    c, sub, payment = _open_checkout(api, make_user, "hs_amount", plan)
    _saved(api, sub.user)
    _account(monkeypatch, [_txn("tx-hs4", "3999.00", sender=ADDRESS)])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    payment.refresh_from_db()
    assert payment.status == "pending"
    assert TransferReview.objects.get(transaction_id="tx-hs4").reason == "no_candidate"


def test_a_near_miss_from_the_saved_address_carries_the_match_as_evidence(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    """The poller's own near miss (the reference, another amount): the queue row and the
    held event say whether it came from the address the customer saved."""
    from subscription.models import TransferReview

    plan = _plan("hs5", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "hs_near", plan)
    _saved(api, sub.user)
    _account(monkeypatch, [_txn("tx-hs5", "9000.00",
                                note=payment.gateway_transaction_reference,
                                sender=ADDRESS)])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["held"] == 1
    review = TransferReview.objects.get(transaction_id="tx-hs5")
    assert review.sender_match_payment_ids == [str(payment.id)]
    assert _events(payment)[-1].evidence["sender_address_matched"] is True


def test_the_console_shows_the_second_signal(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from django.test import Client

    plan = _plan("hs6", "8888.00")
    c, sub, payment = _open_checkout(api, make_user, "hs_console", plan)
    _saved(api, sub.user)
    _account(monkeypatch, [_txn("tx-hs6", "8888.00", sender=ADDRESS)])
    with django_capture_on_commit_callbacks(execute=True):
        _sweep()
    console = Client()
    console.force_login(make_user("hs_operator", user_type="admin"))
    page = console.get("/dj-admin/subscription/transferreview/")
    assert page.status_code == 200
    assert "Sender = saved payer address" in page.content.decode()


# ================================================= 3. "ShamCash is not responding"
def _down(monkeypatch):
    from subscription.gateways.shamcash import ShamCashGateway

    def refuse(self, path, params):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(ShamCashGateway, "_get", refuse)


def _health():
    from subscription.models import GatewayHealth

    return GatewayHealth.objects.get(gateway="shamcash")


def test_a_checkout_answers_the_gateway_state(make_user, api, shamcash):
    plan = _plan("gh0", "10000.00")
    user = _person(make_user, "gh_pay")
    c, sub = _subscribe(api, user, plan)
    body = _checkout(c, sub).json()
    assert body["gateway_state"] == {"state": "ok", "since": None, "last_success_at": None}


def test_the_pollers_record_an_outage_and_every_payment_read_says_so(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    from subscription.models import Payment

    plan = _plan("gh1", "10000.00")
    # Already past its window: only a read that answered may close it.
    c, sub, payment = _open_checkout(api, make_user, "gh_down", plan,
                                     age=timedelta(minutes=90))
    status_url = f"/api/subscription/v1/payments/{payment.id}/status/"

    _down(monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["expired"] == 0
    assert _health().consecutive_failures == 1
    assert c.get(status_url).json()["gateway_state"]["state"] == "ok", "one blip is not an outage"

    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["expired"] == 0
        _sweep()
    health = _health()
    assert health.consecutive_failures == 3 and "connection refused" in health.last_error
    from subscription.models import GatewayHealth
    began = timezone.now() - timedelta(minutes=5)
    GatewayHealth.objects.filter(pk=health.pk).update(failing_since=began)

    state = c.get(status_url).json()["gateway_state"]
    assert state["state"] == "degraded"
    assert state["since"].startswith(began.isoformat()[:16])
    assert state["last_success_at"] is None

    # "Check my payment" during the outage: the money is safe, the answer says why.
    reconcile = _post(c, f"/api/subscription/v1/payments/{payment.id}/reconcile/")
    assert reconcile.status_code == 200
    assert reconcile.json()["status"] == "pending"
    assert reconcile.json()["gateway_state"]["state"] == "degraded"

    payment.refresh_from_db()
    assert payment.status == "pending", "a checkout is never expired because ShamCash is down"
    assert not payment.events.filter(to_status="expired").exists()

    # ShamCash answers again: the state clears, and only now may the empty account close
    # the checkout that outlived its window.
    _account(monkeypatch, [])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["expired"] == 1
    health = _health()
    assert (health.consecutive_failures, health.failing_since) == (0, None)
    assert health.last_success_at is not None
    state = c.get(status_url).json()["gateway_state"]
    assert (state["state"], state["since"]) == ("ok", None)
    assert state["last_success_at"] is not None
    assert Payment.objects.get(pk=payment.pk).status == "expired"


def test_a_transfer_made_during_the_outage_is_found_when_shamcash_answers(
        make_user, api, shamcash, monkeypatch, django_capture_on_commit_callbacks):
    plan = _plan("gh2", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "gh_found", plan,
                                     age=timedelta(minutes=90))
    _down(monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
        _poll()
    _account(monkeypatch, [_txn("tx-gh2", "10000.00",
                                note=payment.gateway_transaction_reference)])
    with django_capture_on_commit_callbacks(execute=True):
        assert _poll()["completed"] == 1
    payment.refresh_from_db()
    assert payment.status == "completed"


def test_the_threshold_is_a_setting(make_user, api, shamcash, monkeypatch, settings,
                                    django_capture_on_commit_callbacks):
    settings.PAYMENT_GATEWAY_DEGRADED_AFTER_FAILURES = 1
    settings.PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS = 0
    plan = _plan("gh3", "10000.00")
    c, sub, payment = _open_checkout(api, make_user, "gh_setting", plan)
    _down(monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
    state = c.get(f"/api/subscription/v1/payments/{payment.id}/status/").json()
    assert state["gateway_state"]["state"] == "degraded"


def test_operators_see_the_gateway_state_in_the_console(
        make_user, api, shamcash, monkeypatch, settings, django_capture_on_commit_callbacks):
    from django.test import Client

    settings.PAYMENT_GATEWAY_DEGRADED_AFTER_SECONDS = 0
    plan = _plan("gh4", "10000.00")
    _open_checkout(api, make_user, "gh_console", plan)
    _down(monkeypatch)
    with django_capture_on_commit_callbacks(execute=True):
        _poll()
        _poll()
    console = Client()
    console.force_login(make_user("gh_operator", user_type="admin"))
    page = console.get("/dj-admin/subscription/gatewayhealth/")
    assert page.status_code == 200
    text = page.content.decode()
    assert "shamcash" in text and "degraded" in text and "connection refused" in text


def test_the_contract_names_the_gateway_state_on_every_payment_read():
    from pathlib import Path

    from subscription.views import (PaymentGatewayView, PaymentReconcileView,
                                    PaymentStatusView, SubscriptionViewSet)

    for doc in (PaymentStatusView.__doc__, PaymentReconcileView.__doc__,
                PaymentGatewayView.post.__doc__, SubscriptionViewSet.renew.__doc__,
                SubscriptionViewSet.upgrade.__doc__):
        assert "gateway_state" in doc
    assert "degraded" in PaymentStatusView.__doc__
    contract = (Path(__file__).resolve().parent.parent / "API_CONTRACT.md").read_text()
    assert "gateway_state" in contract and "/api/subscription/v1/payer-address/" in contract
