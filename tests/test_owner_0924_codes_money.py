"""Every wallet and subscription refusal names its own code (the owner, 2026-09-24).

Decision 9: "Give generic API refusals their own specific error codes. Do it now;
finalize the contract before the Flutter integration gets deeper." Each refusal below
answered only its HTTP status default — `bad_request`, `not_authenticated`,
`permission_denied`, `conflict`, `unprocessable`, `throttled`, or the handler's
`validation_error` for a refusal that is not about a field — so the app could not tell
it from any other refusal with the same status. API_CONTRACT §6 lets such a refusal gain
a code of its own; none of these changes a status or what the request does.

| Test | Endpoint | New code |
|---|---|---|
| `test_role_refusals_...` | agent and operator endpoints | `agents_only`, `admins_only` |
| `test_the_signed_top_up_...` | `POST wallet/agent/topup/` | `agent_auth_invalid`, `agent_ip_not_allowed` |
| `test_the_agent_app_top_up_...` | `POST wallet/agent/topup/proxy/` | `target_not_a_client`, `agent_not_active`, `daily_limit_exceeded`, `monthly_limit_exceeded` |
| `test_an_idempotency_clash_...` | top-up, transfer | `request_in_flight`, `idempotency_key_reused` (see KEY_ON_ANOTHER_WRITE) |
| `test_a_transfer_...` | `POST wallet/client/transfer/` | `clients_only`, `transfer_refused` |
| `test_a_reversal_...` | `POST wallet/admin/reversal/` | `insufficient_balance`, `currency_mismatch`, `reversal_refused` |
| `test_a_second_subscription_...` | `POST subscription/v1/subscriptions/` | `subscription_exists`; field code `invalid_plan` |
| `test_cancelling_...` | `POST .../cancel/` | `not_cancellable`; `validation_error` with `field_errors` |
| `test_an_inactive_subscription_...` | `PATCH .../{id}/` | field code `subscription_inactive` |
| `test_the_operator_trial_...` | `POST subscription/v1/admin/management/` | `missing_fields`, `invalid_user_or_plan`, `subscription_exists`, `invalid_trial_days` |
| `test_the_access_check_...` | `POST subscription/v1/access/check/` | `invalid_features` |
| `test_a_webhook_refusal_...` | `POST subscription/webhook/{gateway}/` | `webhook_not_verified`, `webhook_rejected` |
"""
import json
import time
from decimal import Decimal

import pytest

from tests.test_c9_payments import _signed_webhook, shamcash  # noqa: F401

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal


# --------------------------------------------------------------------- helpers
def _post(client, url, payload=None, **extra):
    return client.post(url, data=json.dumps(payload if payload is not None else {}),
                       content_type=JSON, **extra)


def _agent(make_user, name, *, status="active", **limits):
    from wallet.models import ensure_agent_profile

    agent = make_user(name, user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = status
    for field, value in limits.items():
        setattr(profile, field, D(value) if "limit" in field else value)
    profile.save()
    return agent, profile


def _fund(user, amount):
    from wallet.models import Wallet, wallet_for

    wallet_for(user, user.user_type)
    Wallet.objects.filter(owner=user).update(balance=D(amount))


def _plan(tag, price="5000.00"):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        return SubscriptionPlan.objects.create(
            name=f"codes0924 {tag}", plan_type="premium", description="codes",
            price=D(price), currency="SYP", duration_days=30, is_active=True)


def _code(resp):
    return resp.status_code, resp.json().get("code")


#: An idempotency key already spent on a different request of the caller's own. Minted
#: on 2026-09-24; briefly unified onto `idempotency_conflict` and restored by the review
#: of B-092, because that code means the OTHER person's id (send again under a new one)
#: and this one the caller's own key spent on another request (sending again could
#: move money twice).
KEY_ON_ANOTHER_WRITE = "idempotency_key_reused"


# ======================================================================= wallet
def test_role_refusals_name_the_role_they_need(make_user, api):
    """A client on an agent route and on the operator routes was told `permission_denied`
    — the same code as a paywall or an ownership refusal elsewhere."""
    client = make_user("c0924_role_c")
    c = api(client)

    assert _code(c.get("/api/wallet/agent/apikey/status/")) == (403, "agents_only")
    assert _code(_post(c, "/api/wallet/agent/topup/proxy/",
                       {"client_identifier": "1", "amount": "1.00", "currency": "SYP",
                        "idempotency_key": "k"})) == (403, "agents_only")
    assert _code(_post(c, "/api/wallet/admin/reversal/",
                       {"reference_id": "x", "idempotency_key": "k"})) == (403, "admins_only")
    assert _code(c.get("/api/wallet/admin/audit/export/")) == (403, "admins_only")
    assert _code(c.get("/api/subscription/v1/admin/management/")) == (403, "admins_only")


def test_the_signed_top_up_names_why_it_refused(make_user, api):
    """Every signature failure answered `not_authenticated`, which tells the app to go
    to the login screen — and one code for every kind of failure is kept on purpose."""
    from wallet.models import AgentAPIKey
    from wallet.security import compute_hmac_signature, encrypt_secret

    client = make_user("c0924_sig_c")
    agent, profile = _agent(make_user, "c0924_sig_a", daily_limit="100000",
                            monthly_limit="100000")
    secret = "q" * 64
    AgentAPIKey.objects.create(agent=profile, key_id="c0924key", hashed_key="x",
                               secret_ciphertext=encrypt_secret(secret), is_active=True)
    ts = int(time.time())

    def post(key, *, key_id="c0924key", signature=None, header=True):
        message = f"{client.id}|500.00|{ts}|{key}|SYP"
        sig = signature or compute_hmac_signature(secret, message)
        extra = ({"HTTP_X_AGENT_AUTH": f"AgentAuth key_id={key_id},signature={sig},"
                                        f"timestamp={ts}"} if header else {})
        return _post(api(agent), "/api/wallet/agent/topup/",
                     {"client_identifier": str(client.id), "amount": "500.00",
                      "currency": "SYP", "idempotency_key": key, "timestamp": ts,
                      "signature": sig}, **extra)

    for resp in (post("c0924-1", header=False),
                 post("c0924-2", key_id="nosuchkey"),
                 post("c0924-3", signature="0" * 64)):
        assert _code(resp) == (401, "agent_auth_invalid"), resp.content
        assert resp.json()["detail"], "the message is kept beside the code"

    profile.ip_allowlist = ["10.9.9.9"]
    profile.save()
    assert _code(post("c0924-4")) == (403, "agent_ip_not_allowed")


def test_the_agent_app_top_up_names_why_it_refused(make_user, api):
    from wallet.models import Wallet

    client = make_user("c0924_px_c")
    trainer = make_user("c0924_px_t", user_type="trainer")
    url = "/api/wallet/agent/topup/proxy/"

    def topup(agent, who, key, amount="500.00"):
        return _post(api(agent), url, {"client_identifier": str(who.id), "amount": amount,
                                       "currency": "SYP", "idempotency_key": key})

    agent, _profile = _agent(make_user, "c0924_px_a", daily_limit="100000",
                             monthly_limit="100000")
    assert _code(topup(agent, trainer, "px-1")) == (400, "target_not_a_client")

    daily, _p = _agent(make_user, "c0924_px_d", daily_limit="100", monthly_limit="100000")
    assert _code(topup(daily, client, "px-2")) == (429, "daily_limit_exceeded")

    monthly, _p = _agent(make_user, "c0924_px_m", daily_limit="100000", monthly_limit="100")
    assert _code(topup(monthly, client, "px-3")) == (429, "monthly_limit_exceeded")

    suspended, _p = _agent(make_user, "c0924_px_s", status="suspended",
                           daily_limit="100000", monthly_limit="100000")
    assert _code(topup(suspended, client, "px-4")) == (403, "agent_not_active")

    assert not Wallet.objects.filter(owner=client, balance__gt=0).exists(), \
        "a refusal moved money"


def test_an_idempotency_clash_says_whether_retrying_helps(make_user, api):
    """409 and 422 were `conflict` and `unprocessable`: the app could not tell "wait for
    the first attempt" from "this key is spent on another request"."""
    from wallet.models import IdempotencyKey

    client = make_user("c0924_idem_c")
    agent, _profile = _agent(make_user, "c0924_idem_a", daily_limit="100000",
                             monthly_limit="100000")
    url = "/api/wallet/agent/topup/proxy/"
    body = {"client_identifier": str(client.id), "currency": "SYP",
            "idempotency_key": "idem-1"}

    first = _post(api(agent), url, {**body, "amount": "10.00"})
    assert first.status_code == 200, first.content
    assert _code(_post(api(agent), url, {**body, "amount": "20.00"})) == \
        (422, KEY_ON_ANOTHER_WRITE)

    # A first attempt still running: the row is claimed and not yet finished.
    trainer = make_user("c0924_idem_t", user_type="trainer")
    IdempotencyKey.objects.create(created_by=client, key="idem-2", request_hash="")
    resp = _post(api(client), "/api/wallet/client/transfer/",
                 {"trainer_id": trainer.id, "amount": "10.00", "idempotency_key": "idem-2"})
    assert _code(resp) == (409, "request_in_flight")


def test_a_transfer_names_why_it_refused(make_user, api, monkeypatch):
    from wallet import views as wallet_views

    trainer = make_user("c0924_tr_t", user_type="trainer")
    other = make_user("c0924_tr_o", user_type="trainer")
    resp = _post(api(other), "/api/wallet/client/transfer/",
                 {"trainer_id": trainer.id, "amount": "10.00", "idempotency_key": "tr-1"})
    assert _code(resp) == (403, "clients_only")

    # Any other refusal from the ledger. Nothing the endpoint validates today reaches
    # it, so the ledger is made to refuse: the subject is what the view SAYS.
    client = make_user("c0924_tr_c")
    _fund(client, "100.00")

    def refuses(*args, **kwargs):
        raise ValueError("refused by the ledger")

    monkeypatch.setattr(wallet_views, "move_funds_atomic", refuses)
    resp = _post(api(client), "/api/wallet/client/transfer/",
                 {"trainer_id": trainer.id, "amount": "10.00", "idempotency_key": "tr-2"})
    assert _code(resp) == (400, "transfer_refused")


def test_a_reversal_names_why_the_ledger_refused_it(make_user, api, monkeypatch):
    """Three different reasons were one `bad_request`: the trainer has already spent the
    money, a wallet changed currency, anything else."""
    from policy.money import MoneyType
    from wallet import views as wallet_views
    from wallet.models import Transaction, Wallet, move_funds_atomic, wallet_for

    admin = make_user("c0924_rv_admin", user_type="admin")
    client = make_user("c0924_rv_c")
    trainer = make_user("c0924_rv_t", user_type="trainer")
    _fund(client, "300.00")
    wallet_for(trainer, "trainer")

    def paid(amount):
        return move_funds_atomic(Wallet.objects.get(owner=client),
                                 Wallet.objects.get(owner=trainer), D(amount),
                                 actor_id=client.id, tx_type="transfer",
                                 money_type=MoneyType.WALLET_MOVE)

    def reverse(tx, key):
        return _post(api(admin), "/api/wallet/admin/reversal/",
                     {"reference_id": tx.reference_id, "idempotency_key": key})

    spent = paid("50.00")
    Wallet.objects.filter(owner=trainer).update(balance=D("0"))
    assert _code(reverse(spent, "rv-1")) == (400, "insufficient_balance")

    moved = paid("50.00")
    Wallet.objects.filter(owner=trainer).update(currency="USD")
    assert _code(reverse(moved, "rv-2")) == (400, "currency_mismatch")
    Wallet.objects.filter(owner=trainer).update(currency="SYP")

    def refuses(*args, **kwargs):
        raise ValueError("refused by the ledger")

    monkeypatch.setattr(wallet_views, "move_funds_atomic", refuses)
    assert _code(reverse(moved, "rv-3")) == (400, "reversal_refused")
    assert not Transaction.objects.filter(tx_type="reversal").exists()


# ================================================================= subscription
def test_a_second_subscription_is_told_apart_from_a_bad_plan(make_user, api):
    """Both answered `validation_error`; the one with a next step (change-plan, upgrade,
    renew) now says so. A bad `plan_id` stays a field error, with the field code the
    change-plan and upgrade routes answer for the same fact."""
    user = make_user("c0924_sub_c")
    plan = _plan("one")
    c = api(user)
    url = "/api/subscription/v1/subscriptions/"

    assert _post(c, url, {"plan_id": plan.id}).status_code == 201
    again = _post(c, url, {"plan_id": plan.id})
    assert _code(again) == (400, "subscription_exists"), again.content
    assert again.json()["detail"]

    other = make_user("c0924_sub_o")
    bad = _post(api(other), url, {"plan_id": 987654})
    assert _code(bad) == (400, "validation_error")
    assert bad.json()["field_errors"]["plan_id"][0]["code"] == "invalid_plan"


def test_cancelling_twice_says_so_and_a_bad_body_is_a_field_error(make_user, api):
    """The view RETURNED `serializer.errors`, past the exception handler: no `detail`,
    no `field_errors`, and `bad_request` for the one refusal the app can act on."""
    from subscription.models import Subscription

    user = make_user("c0924_cn_c")
    plan = _plan("cancel")
    c = api(user)
    assert _post(c, "/api/subscription/v1/subscriptions/", {"plan_id": plan.id}).status_code == 201
    sub = Subscription.objects.get(user=user)
    url = f"/api/subscription/v1/subscriptions/{sub.id}/cancel/"

    long = _post(c, url, {"reason": "x" * 501})
    assert _code(long) == (400, "validation_error"), long.content
    assert "reason" in long.json()["field_errors"]

    assert _post(c, url, {"immediate": True}).status_code == 200
    twice = _post(c, url, {"immediate": True})
    assert _code(twice) == (400, "not_cancellable"), twice.content
    body = twice.json()
    assert body["detail"] and body["non_field_errors"], "the shipped key is kept"


def test_an_inactive_subscription_says_why_it_cannot_be_updated(make_user, api):
    """The contract names `validation_error` here, so it stays; the reason is the field
    code, which was DRF's `invalid`."""
    from subscription.models import Subscription

    user = make_user("c0924_up_c")
    plan = _plan("update")
    c = api(user)
    assert _post(c, "/api/subscription/v1/subscriptions/", {"plan_id": plan.id}).status_code == 201
    sub = Subscription.objects.get(user=user)
    assert sub.status == "pending"

    resp = c.patch(f"/api/subscription/v1/subscriptions/{sub.id}/", data="{}",
                   content_type=JSON)
    assert _code(resp) == (400, "validation_error"), resp.content
    assert resp.json()["field_errors"]["non_field_errors"][0]["code"] == \
        "subscription_inactive"


def test_the_operator_trial_names_each_refusal(make_user, api):
    from subscription.models import Subscription

    admin = make_user("c0924_mg_admin", user_type="admin")
    plan = _plan("trial")
    target = make_user("c0924_mg_c")
    c = api(admin)
    url = "/api/subscription/v1/admin/management/"

    assert _code(_post(c, url, {"plan_id": plan.id})) == (400, "missing_fields")
    assert _code(_post(c, url, {"user_id": 99999999, "plan_id": plan.id})) == \
        (400, "invalid_user_or_plan")
    assert _code(_post(c, url, {"user_id": target.id, "plan_id": plan.id,
                                "trial_days": -3})) == (400, "invalid_trial_days")
    assert not Subscription.objects.filter(user=target).exists()

    assert _post(c, url, {"user_id": target.id, "plan_id": plan.id}).status_code == 201
    assert _code(_post(c, url, {"user_id": target.id, "plan_id": plan.id})) == \
        (400, "subscription_exists")


def test_the_access_check_names_a_bad_features_list(make_user, api):
    user = make_user("c0924_ac_c")
    resp = _post(api(user), "/api/subscription/v1/access/check/", {"features": "diet"})
    assert _code(resp) == (400, "invalid_features")


def test_a_webhook_refusal_says_whether_it_was_verified(shamcash):  # noqa: F811
    """Both were empty 400s. The status — all a gateway reads — is unchanged."""
    from django.test import Client

    unsigned = Client().post("/api/subscription/webhook/shamcash/",
                             data=json.dumps({"data": {"reference": "x"}}),
                             content_type=JSON)
    assert _code(unsigned) == (400, "webhook_not_verified"), unsigned.content

    unknown = _signed_webhook("C0924-NO-SUCH-REFERENCE")
    assert _code(unknown) == (400, "webhook_rejected"), unknown.content
