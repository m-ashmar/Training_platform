"""BACKEND_BUGS B-199: what each build answers on every path that moves WALLET_MOVE money.

`policy/services.py` maps WALLET_MOVE to `can_transfer_wallet`, and the spec's table
(`policy/defaults.py`) has it on for `apk`, `web` and `play` and OFF for the first `ios`
build. `AgentTopUpProxyView`'s docstring said "every build allows it": the flag is what
runs. This pins the table as it stands, path by path, with the code each refusal
answers, and WHOSE build each path reads: always the build of the request that moves the
money, never the receiver's.

| Path | Whose build | apk | web | play | ios | none |
|---|---|---|---|---|---|---|
| `POST wallet/agent/topup/proxy/` (the agent's app) | the agent's | 200 | 200 | 200 | 403 `not_available_on_channel` | 400 `app_channel_required` |
| `POST wallet/agent/topup/` (signed, the agent's server) | none: no build is read | 200 | 200 | 200 | 200 | 200 |
| `POST wallet/client/transfer/` (to a trainer; also COACHING_1TO1) | the client's | 200 | 200 | 200 | 403 `not_available_on_channel` | 400 `app_channel_required` |
| `POST v1/trainer/withdrawals/` | the trainer's | 201 | 201 | 201 | 403 `not_available_on_channel` | 400 `app_channel_required` |
| `POST v1/trainer/withdrawals/{id}/cancel/` | the trainer's | 200 | 200 | 200 | 403 `not_available_on_channel` | 400 `app_channel_required` |
| `GET v1/trainer/withdrawals/setup/` `refusal` | the trainer's | null | null | null | `not_available_on_channel` | `app_channel_required` |

There is no operator top-up: an operator records a settlement in the console
(`/dj-admin/`), and the operator's phone API (`admin_dashboard/ops_api.py`) shows queue
counts and signs the console in; neither moves wallet money. The client being topped up
sends no request, so a client on an `ios` build is credited by an agent on any build
that allows it, and reads the balance there (`can_view_wallet` is on everywhere).

No behaviour is changed here: which of the docstring and the flag the owner meant is his
question (B-199). A refused request writes nothing and moves nothing.
"""
import time
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
BUILDS = ("apk", "web", "play", "ios", None)
WITHDRAWALS = "/api/v1/trainer/withdrawals/"
METHODS = "/api/v1/trainer/payout-methods/"
ADDRESS = "0a3f 91c2 7be4 55d0"

ALLOWED = {"apk", "web", "play"}


def _expected(build, ok_status):
    if build in ALLOWED:
        return ok_status, None
    if build == "ios":
        return 403, "not_available_on_channel"
    return 400, "app_channel_required"


def _on(build):
    """The header a build sends; `""` is a caller that declares none."""
    return {"HTTP_X_APP_CHANNEL": build or ""}


def _code(resp):
    body = resp.json() if resp.content else {}
    return resp.status_code, (body.get("code") if resp.status_code >= 400 else None)


def _tag(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def base_rows(db):
    """The spec's rows, as the seed migration writes them (a thread test may have
    flushed them; the defaults answer the same either way)."""
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities

    for channel, flags in DEFAULT_FLAGS.items():
        ChannelCapabilities.objects.get_or_create(
            channel=channel, min_version="0.0.0", defaults=flags)


def _client(make_user, tag):
    from wallet.models import Wallet, wallet_for

    client = make_user(_tag(f"b199_{tag}_c"))
    wallet_for(client, "client")
    Wallet.objects.filter(owner=client).update(balance=D("0.00"))
    return client


def _agent(make_user, tag):
    from wallet.models import ensure_agent_profile

    agent = make_user(_tag(f"b199_{tag}_a"), user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    profile.daily_limit = profile.monthly_limit = D("10000000")
    profile.save()
    return agent, profile


def _balance(user):
    from wallet.models import Wallet

    return Wallet.objects.get(owner=user).balance


# ------------------------------------------------------------------- the agent's app
@pytest.mark.parametrize("build", BUILDS)
def test_the_agent_apps_top_up_reads_the_agents_build(make_user, api, build):
    client = _client(make_user, "px")
    agent, _profile = _agent(make_user, "px")
    resp = api(agent).post("/api/wallet/agent/topup/proxy/",
                           {"client_identifier": str(client.pk), "amount": "500.00",
                            "currency": "SYP", "idempotency_key": _tag("px")},
                           content_type=JSON, **_on(build))
    assert _code(resp) == _expected(build, 200), resp.content
    assert _balance(client) == (D("500.00") if build in ALLOWED else D("0.00"))


# ------------------------------------------------------------------ the agent's server
@pytest.mark.parametrize("build", BUILDS)
def test_the_signed_top_up_reads_no_build(make_user, api, build):
    from wallet.models import AgentAPIKey
    from wallet.security import compute_hmac_signature, encrypt_secret

    client = _client(make_user, "sig")
    agent, profile = _agent(make_user, "sig")
    secret = "s" * 64
    key_id = _tag("b199key")
    AgentAPIKey.objects.create(agent=profile, key_id=key_id, hashed_key="x",
                               secret_ciphertext=encrypt_secret(secret), is_active=True)
    ts, key = int(time.time()), _tag("sig")
    sig = compute_hmac_signature(secret, f"{client.pk}|500.00|{ts}|{key}|SYP")
    resp = api(agent).post(
        "/api/wallet/agent/topup/",
        {"client_identifier": str(client.pk), "amount": "500.00", "currency": "SYP",
         "idempotency_key": key, "timestamp": ts, "signature": sig},
        content_type=JSON,
        HTTP_X_AGENT_AUTH=f"AgentAuth key_id={key_id},signature={sig},timestamp={ts}",
        **_on(build))
    assert _code(resp) == (200, None), resp.content
    assert _balance(client) == D("500.00")


def test_a_client_on_ios_is_topped_up_by_an_agent_on_another_build(make_user, api):
    """The client sends nothing: the agent's build is the one asked. The client reads
    the new balance on ios, where the wallet is shown."""
    client = _client(make_user, "cross")
    on_apk, _p = _agent(make_user, "cross_apk")
    on_ios, _p = _agent(make_user, "cross_ios")
    body = {"client_identifier": str(client.pk), "amount": "700.00", "currency": "SYP"}

    refused = api(on_ios).post("/api/wallet/agent/topup/proxy/",
                               {**body, "idempotency_key": _tag("x")},
                               content_type=JSON, **_on("ios"))
    assert _code(refused) == (403, "not_available_on_channel")
    done = api(on_apk).post("/api/wallet/agent/topup/proxy/",
                            {**body, "idempotency_key": _tag("x")},
                            content_type=JSON, **_on("apk"))
    assert done.status_code == 200, done.content

    wallet = api(client).get("/api/wallet/balance/", **_on("ios"))
    assert wallet.status_code == 200, wallet.content
    assert D(str(wallet.json()["balance"])) == D("700.00")


# ------------------------------------------------------------- a transfer to a trainer
@pytest.mark.parametrize("build", BUILDS)
def test_a_transfer_reads_the_clients_build(make_user, api, build):
    from tests.test_routine_products import _fund

    client = make_user(_tag("b199_tr_c"))
    trainer = make_user(_tag("b199_tr_t"), user_type="trainer")
    _fund(client, "8000.00", "client")
    _fund(trainer, "0.00", "trainer")
    resp = api(client).post("/api/wallet/client/transfer/",
                            {"trainer_id": trainer.pk, "amount": "100.00",
                             "idempotency_key": _tag("tr")},
                            content_type=JSON, **_on(build))
    assert _code(resp) == _expected(build, 200), resp.content
    assert _balance(client) == (D("7900.00") if build in ALLOWED else D("8000.00"))


# --------------------------------------------------------------- a trainer's withdrawal
def _ready_trainer(make_user, api):
    from wallet.models import Wallet

    trainer = make_user(_tag("b199_w_t"), user_type="trainer", trainer_is_verified=True,
                        first_name="Rami")
    wallet, _created = Wallet.objects.get_or_create(owner=trainer,
                                                    defaults={"owner_type": "trainer"})
    wallet.balance = D("100000.00")
    wallet.save()
    added = api(trainer).post(METHODS, {"kind": "shamcash", "address": ADDRESS,
                                        "address_confirm": ADDRESS,
                                        "holder_name": "رامي حداد"}, content_type=JSON)
    assert added.status_code == 201, added.content
    return trainer


@pytest.mark.parametrize("build", BUILDS)
def test_a_withdrawal_and_its_cancel_read_the_trainers_build(make_user, api, build):
    from payouts.models import Withdrawal

    trainer = _ready_trainer(make_user, api)
    setup = api(trainer).get(f"{WITHDRAWALS}setup/", **_on(build))
    assert setup.status_code == 200, setup.content
    assert setup.json()["refusal"] == _expected(build, None)[1]

    asked = api(trainer).post(WITHDRAWALS, {"amount": "30000",
                                            "idempotency_key": _tag("w")},
                              content_type=JSON, **_on(build))
    assert _code(asked) == _expected(build, 201), asked.content
    assert Withdrawal.objects.filter(trainer=trainer).exists() is (build in ALLOWED)

    # The cancel, of a withdrawal the direct APK asked for.
    if build not in ALLOWED:
        asked = api(trainer).post(WITHDRAWALS, {"amount": "30000",
                                                "idempotency_key": _tag("w")},
                                  content_type=JSON, **_on("apk"))
        assert asked.status_code == 201, asked.content
    wid = asked.json()["id"]
    cancelled = api(trainer).post(f"{WITHDRAWALS}{wid}/cancel/", {}, content_type=JSON,
                                  **_on(build))
    assert _code(cancelled) == _expected(build, 200), cancelled.content
    assert (Withdrawal.objects.get(pk=wid).state != "requested") is (build in ALLOWED)


def test_the_table_is_the_spec_s_own():
    """What the paths above follow: WALLET_MOVE is `can_transfer_wallet`, on for apk,
    web and play and off for the first ios build."""
    from policy.defaults import DEFAULT_FLAGS
    from policy.money import MoneyType
    from policy.services import PURCHASE_FLAG

    assert PURCHASE_FLAG[MoneyType.WALLET_MOVE] == "can_transfer_wallet"
    assert {c for c, flags in DEFAULT_FLAGS.items() if flags["can_transfer_wallet"]} == \
        ALLOWED
