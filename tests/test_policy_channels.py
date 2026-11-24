"""What each build may show and sell — the platform policy switch.

The rule these tests hold: the server's behaviour depends on the BUILD that is calling
(`X-App-Channel`, `X-App-Version`) and on nothing else. A store build can never open a
checkout for a digital good, whatever a database row says, and two users of one build
can never be told different things.
"""
import json
from decimal import Decimal

import pytest

CAPABILITIES = "/api/v1/config/capabilities"
NOT_AVAILABLE = "not_available_on_channel"
CHANNEL_REQUIRED = "app_channel_required"

#: The spec's table, section 4. "Coaching only" prices on Play are the pair
#: can_show_prices=False + can_show_coaching_prices=True.
SPEC_TABLE = {
    "apk": dict(can_purchase_platform_sub=True, can_purchase_routine_product=True,
                can_pay_coaching_in_app=True, can_show_prices=True,
                can_show_coaching_prices=True, can_show_web_hub_link=True,
                can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
    "play": dict(can_purchase_platform_sub=False, can_purchase_routine_product=False,
                 can_pay_coaching_in_app=True, can_show_prices=False,
                 can_show_coaching_prices=True, can_show_web_hub_link=False,
                 can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
    "ios": dict(can_purchase_platform_sub=False, can_purchase_routine_product=False,
                can_pay_coaching_in_app=False, can_show_prices=False,
                can_show_coaching_prices=False, can_show_web_hub_link=False,
                can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=False),
    "web": dict(can_purchase_platform_sub=True, can_purchase_routine_product=True,
                can_pay_coaching_in_app=True, can_show_prices=True,
                can_show_coaching_prices=True, can_show_web_hub_link=False,
                can_view_entitlements=True, can_view_wallet=True, can_transfer_wallet=True),
}

#: The one part of the answer that is not a flag. A deliberate, additive contract change
#: (spec B7, 2026-09-27): the capabilities answer carries the public pages' URLs so they
#: can move without a release — the same for every channel and version, built from the
#: test server's host (`legal/links.py`). The exact-answer tests below still assert the
#: WHOLE answer, flags included; tests/test_releases_legal.py covers the links themselves.
LINKS = {
    page: {lang: f"http://testserver/legal/{path}/?lang={lang}" for lang in ("ar", "en")}
    for page, path in (("privacy_policy", "privacy"), ("terms_of_use", "terms"),
                       ("account_deletion", "delete-account"))
}


# --------------------------------------------------------------------------- helpers
@pytest.fixture(autouse=True)
def base_rows(db):
    """The rows the seed migration writes. Put back here because the thread-based tests
    flush every table, and a reused database starts the next run without them."""
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities

    for channel, flags in DEFAULT_FLAGS.items():
        ChannelCapabilities.objects.get_or_create(
            channel=channel, min_version="0.0.0", defaults=flags)


@pytest.fixture
def shamcash():
    """ShamCash configured, so a checkout that is ALLOWED actually opens."""
    from subscription.settings import gateway_config

    config = gateway_config.GATEWAY_REGISTRY["shamcash"]["config"][gateway_config.GATEWAY_MODE]
    before = dict(config)
    config["merchant_id"] = "POLICY-ACCOUNT-1"
    config["api_key"] = "policy-api-token"          # what lists the gateway as enabled
    # With the API's base URL, which has no default since 2026-11-24 (no third party):
    # a token alone calls nothing and enables nothing.
    config["api_url"] = "https://shamcash.test/v1"
    config["webhook_secret"] = "policy-webhook-secret-0123456789abcdef"
    config["initiate_path"] = ""
    try:
        yield config
    finally:
        config.clear()
        config.update(before)


def _plan(tag, price="5000.00"):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        return SubscriptionPlan.objects.create(
            name=f"policy {tag}", plan_type="premium", description="policy",
            price=Decimal(price), currency="SYP", duration_days=30, is_active=True)


def _post(client, url, payload=None, **extra):
    return client.post(url, data=json.dumps(payload or {}),
                       content_type="application/json", **extra)


def _on(channel, version=None):
    extra = {"HTTP_X_APP_CHANNEL": channel}
    if version is not None:
        extra["HTTP_X_APP_VERSION"] = version
    return extra


# ------------------------------------------------------------- the capabilities endpoint
@pytest.mark.django_db
@pytest.mark.parametrize("channel", sorted(SPEC_TABLE))
def test_each_channel_gets_the_flags_the_spec_sets(client, channel):
    body = client.get(CAPABILITIES, **_on(channel)).json()
    assert body == {"channel": channel, **SPEC_TABLE[channel], "links": LINKS}


def test_the_seed_migration_and_the_code_defaults_are_the_same_table():
    import importlib

    from policy.defaults import DEFAULT_FLAGS

    seed = importlib.import_module("policy.migrations.0002_seed_channel_capabilities")
    assert seed.BASE_ROWS == DEFAULT_FLAGS == SPEC_TABLE


@pytest.mark.django_db
@pytest.mark.parametrize("channel", sorted(SPEC_TABLE))
def test_a_channel_with_no_row_gets_the_specs_table_not_a_shutdown(client, channel):
    """Losing the rows must not stop the direct APK selling — and cannot start a store
    build selling, because that is locked in code."""
    from policy.models import ChannelCapabilities

    ChannelCapabilities.objects.all().delete()
    assert client.get(CAPABILITIES, **_on(channel)).json() == {
        "channel": channel, **SPEC_TABLE[channel], "links": LINKS}


@pytest.mark.django_db
@pytest.mark.parametrize("channel", ["ios", "play", "apk"])
def test_two_users_and_a_visitor_on_one_build_get_identical_flags(make_user, api, channel):
    """Acceptance: flags are identical for different users on the same channel and
    version. A reviewer's account and a customer's account are two such users."""
    admin = make_user(f"pol_same_admin_{channel}", user_type="admin")
    customer = make_user(f"pol_same_client_{channel}", user_type="client")
    answers = [who.get(CAPABILITIES, **_on(channel, "1.0.0")).json()
               for who in (api(admin), api(customer), api())]
    assert answers[0] == answers[1] == answers[2]


@pytest.mark.django_db
@pytest.mark.parametrize("declared", ["", "iOS", "appstore", "ios "])
def test_a_caller_that_declares_no_known_channel_is_told_so(client, declared):
    """Never guessed: a channel the server does not know is a channel not declared.
    (Surrounding whitespace is a header artefact and is not a different channel.)"""
    resp = client.get(CAPABILITIES, HTTP_X_APP_CHANNEL=declared)
    if declared.strip() == "ios":
        assert resp.status_code == 200
    else:
        assert resp.status_code == 400 and resp.json()["code"] == CHANNEL_REQUIRED


@pytest.mark.django_db
def test_a_newer_build_takes_the_row_written_for_it(client):
    """iOS coaching is enabled for a LATER, separately reviewed build by adding a row —
    the build already in the store keeps the behaviour it was reviewed with."""
    from policy.models import ChannelCapabilities

    ChannelCapabilities(channel="ios", min_version="2.0", can_pay_coaching_in_app=True,
                        can_show_coaching_prices=True, can_transfer_wallet=True).save()

    def coaching(version):
        return client.get(CAPABILITIES, **_on("ios", version)).json()["can_pay_coaching_in_app"]

    assert coaching("1.9.9") is False
    assert coaching("2.0.0") is True
    assert coaching("2.3.1+45") is True     # Flutter's version+build form
    assert coaching(None) is False          # no version: the channel's base row
    assert coaching("not-a-version") is False


# ------------------------------------------------------ what a store build can never do
@pytest.mark.django_db
@pytest.mark.parametrize("channel", ["ios", "play"])
def test_a_plan_checkout_from_a_store_build_is_refused(make_user, api, shamcash, channel):
    """Acceptance: a PLATFORM_SUB checkout with channel ios or play is refused by the
    server. Every door: choosing a plan, the Pay button, and Renew."""
    from subscription.models import Payment, Subscription

    plan = _plan(f"refused {channel}")
    user = make_user(f"pol_refused_{channel}", user_type="client")
    c = api(user)

    resp = _post(c, "/api/subscription/v1/subscriptions/", {"plan_id": plan.id}, **_on(channel))
    assert resp.status_code == 403 and resp.json()["code"] == NOT_AVAILABLE, resp.content
    assert not Subscription.objects.filter(user=user).exists()

    # The same user subscribes on the direct APK; the store build still cannot pay or renew.
    assert _post(c, "/api/subscription/v1/subscriptions/", {"plan_id": plan.id}).status_code == 201
    sub = Subscription.objects.get(user=user)
    before = Payment.objects.filter(subscription=sub).count()

    pay = _post(c, "/api/subscription/v1/gateways/",
                {"gateway": "shamcash", "subscription_id": str(sub.id)}, **_on(channel))
    renew = _post(c, f"/api/subscription/v1/subscriptions/{sub.id}/renew/",
                  {"gateway": "shamcash"}, **_on(channel))
    for resp in (pay, renew):
        assert resp.status_code == 403 and resp.json()["code"] == NOT_AVAILABLE, resp.content
    assert Payment.objects.filter(subscription=sub).count() == before
    assert not Payment.objects.filter(subscription=sub).exclude(
        gateway_transaction_reference__isnull=True).exists()

    # And the direct APK can, so the refusal above is the channel and nothing else.
    ok = _post(c, "/api/subscription/v1/gateways/",
               {"gateway": "shamcash", "subscription_id": str(sub.id)})
    assert ok.status_code == 200 and ok.json()["reference"], ok.content


@pytest.mark.django_db
@pytest.mark.parametrize("channel", ["ios", "play"])
def test_a_row_that_says_yes_cannot_switch_purchasing_on_in_a_store_build(
        make_user, api, client, shamcash, channel):
    """The refusal is in code. A row written around the model — SQL, `update()`, a bad
    fixture — is masked when read and ignored when a checkout is asked for."""
    from policy.models import ChannelCapabilities

    ChannelCapabilities.objects.filter(channel=channel).update(
        can_purchase_platform_sub=True, can_purchase_routine_product=True,
        can_show_prices=True, can_show_web_hub_link=True)

    flags = client.get(CAPABILITIES, **_on(channel)).json()
    assert [flags[k] for k in ("can_purchase_platform_sub", "can_purchase_routine_product",
                               "can_show_prices", "can_show_web_hub_link")] == [False] * 4

    plan = _plan(f"forced {channel}")
    user = make_user(f"pol_forced_{channel}", user_type="client")
    resp = _post(api(user), "/api/subscription/v1/subscriptions/", {"plan_id": plan.id},
                 **_on(channel))
    assert resp.status_code == 403 and resp.json()["code"] == NOT_AVAILABLE


@pytest.mark.django_db
def test_an_operator_cannot_save_purchasing_on_for_a_store_build():
    from django.core.exceptions import ValidationError

    from policy.models import ChannelCapabilities

    row = ChannelCapabilities.objects.get(channel="ios", min_version="0.0.0")
    row.can_purchase_platform_sub = True
    with pytest.raises(ValidationError):
        row.save()
    row.refresh_from_db()
    assert row.can_purchase_platform_sub is False

    # The direct APK is ours to configure: the same flag is editable there.
    apk = ChannelCapabilities.objects.get(channel="apk", min_version="0.0.0")
    apk.can_purchase_platform_sub = False
    apk.save()


@pytest.mark.django_db
def test_no_declared_channel_no_checkout(make_user, api, shamcash):
    plan = _plan("undeclared")
    user = make_user("pol_undeclared", user_type="client")
    resp = _post(api(user), "/api/subscription/v1/subscriptions/", {"plan_id": plan.id},
                 HTTP_X_APP_CHANNEL="")
    assert resp.status_code == 400 and resp.json()["code"] == CHANNEL_REQUIRED


@pytest.mark.django_db
def test_a_free_plan_is_not_a_purchase(make_user, api):
    plan = _plan("free", price="0.00")
    user = make_user("pol_free_ios", user_type="client")
    resp = _post(api(user), "/api/subscription/v1/subscriptions/", {"plan_id": plan.id},
                 **_on("ios"))
    assert resp.status_code == 201, resp.content


# --------------------------------------------------- prices and payment instructions
@pytest.mark.django_db
def test_plan_prices_are_not_sent_to_a_build_that_may_not_show_them(make_user, api, shamcash):
    """Not sent, rather than sent and trusted to be hidden. What the user HOLDS — plan,
    status, expiry, source — still reaches every build."""
    plan = _plan("priced")
    user = make_user("pol_priced", user_type="client")
    c = api(user)
    assert _post(c, "/api/subscription/v1/subscriptions/", {"plan_id": plan.id}).status_code == 201

    def catalogue(channel):
        rows = c.get("/api/subscription/v1/plans/", **_on(channel)).json()
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        return next(r for r in rows if r["id"] == plan.id)

    assert Decimal(catalogue("apk")["price"]) == Decimal("5000.00")
    assert Decimal(catalogue("web")["price"]) == Decimal("5000.00")
    for channel in ("ios", "play", ""):
        assert "price" not in catalogue(channel), channel

    detail = c.get(f"/api/subscription/v1/plans/{plan.id}/", **_on("ios")).json()
    assert "price" not in detail and detail["name"]

    mine_ios = c.get("/api/subscription/v1/subscriptions/current/", **_on("ios")).json()
    mine_apk = c.get("/api/subscription/v1/subscriptions/current/").json()
    assert "plan_price" not in mine_ios and mine_ios["plan_name"] and "end_date" in mine_ios
    assert Decimal(mine_apk["plan_price"]) == Decimal("5000.00")


@pytest.mark.django_db
def test_how_to_pay_is_not_told_to_a_build_that_cannot_sell(make_user, api, shamcash):
    c = api(make_user("pol_gateways", user_type="client"))
    assert c.get("/api/subscription/v1/gateways/").json()["gateways"]
    for channel in ("ios", "play"):
        assert c.get("/api/subscription/v1/gateways/", **_on(channel)).json()["gateways"] == []


# ------------------------------------------------------------ coaching and the wallet
@pytest.mark.django_db
def test_a_wallet_transfer_follows_the_builds_flag(make_user, api):
    from wallet.models import move_funds_atomic, wallet_for

    client_user = make_user("pol_transfer_c", user_type="client")
    trainer = make_user("pol_transfer_t", user_type="trainer")
    wallet = wallet_for(client_user, "client")
    move_funds_atomic(None, wallet, Decimal("300.00"), tx_type="topup")

    def transfer(key, **extra):
        return _post(api(client_user), "/api/wallet/client/transfer/",
                     {"trainer_id": trainer.id, "amount": "100.00", "idempotency_key": key},
                     **extra)

    refused = transfer("pol-transfer-ios", **_on("ios"))
    assert refused.status_code == 403 and refused.json()["code"] == NOT_AVAILABLE
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("300.00")

    assert transfer("pol-transfer-play", **_on("play")).status_code == 200
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("200.00")


@pytest.mark.django_db
def test_asking_a_trainer_who_charges_follows_the_builds_flag(make_user, api):
    """iOS v1 has no coaching payments; Play and the APK do. A trainer who charges
    nothing can be asked from any build, because nothing is being paid."""
    from users.models import TrainerClientRelation

    paid = make_user("pol_coach_paid", user_type="trainer", trainer_hourly_rate=Decimal("100.00"),
                     trainer_is_available=True)
    free = make_user("pol_coach_free", user_type="trainer", trainer_hourly_rate=Decimal("0"),
                     trainer_is_available=True)
    client_user = make_user("pol_coach_client", user_type="client")
    c = api(client_user)

    refused = _post(c, "/api/auth/client/request-trainer/", {"trainer_id": paid.id}, **_on("ios"))
    assert refused.status_code == 403 and refused.json()["code"] == NOT_AVAILABLE, refused.content
    assert not TrainerClientRelation.objects.filter(client=client_user).exists()

    asked = _post(c, "/api/auth/client/request-trainer/", {"trainer_id": free.id}, **_on("ios"))
    assert asked.status_code == 200, asked.content


# ------------------------------------------------------------------- the change log
@pytest.mark.django_db
def test_every_flag_change_is_logged_with_who_and_what(make_user):
    from policy.models import CapabilityChangeLog, ChannelCapabilities

    operator = make_user("pol_operator", user_type="admin")
    row = ChannelCapabilities.objects.get(channel="play", min_version="0.0.0")
    start = CapabilityChangeLog.objects.count()

    row.save(changed_by=operator)                       # nothing changed: nothing logged
    assert CapabilityChangeLog.objects.count() == start

    row.can_transfer_wallet = False
    row.save(changed_by=operator)
    entry = CapabilityChangeLog.objects.order_by("-id").first()
    assert (entry.channel, entry.min_version, entry.action) == ("play", "0.0.0", "changed")
    assert entry.changed_by == operator and entry.created_at is not None
    assert entry.old_values["can_transfer_wallet"] is True
    assert entry.new_values["can_transfer_wallet"] is False

    added = ChannelCapabilities(channel="play", min_version="3.1")
    added.save(changed_by=operator)
    assert added.min_version == "3.1.0"                 # one spelling per version
    assert CapabilityChangeLog.objects.order_by("-id").first().action == "created"
    added.delete(changed_by=operator)
    gone = CapabilityChangeLog.objects.order_by("-id").first()
    assert gone.action == "deleted" and gone.old_values and gone.new_values == {}

    with pytest.raises(PermissionError):
        entry.delete()
    entry.action = "created"
    with pytest.raises(PermissionError):
        entry.save()


@pytest.mark.django_db
def test_the_operator_console_names_the_operator(make_user):
    """The console's save goes through `save(changed_by=...)`; without that the log would
    record every console edit as made by nobody."""
    from django.contrib.admin.sites import AdminSite
    from django.test import RequestFactory

    from policy.admin import ChannelCapabilitiesAdmin
    from policy.models import CapabilityChangeLog, ChannelCapabilities

    operator = make_user("pol_console_operator", user_type="admin")
    request = RequestFactory().post("/dj-admin/policy/channelcapabilities/")
    request.user = operator
    row = ChannelCapabilities.objects.get(channel="apk", min_version="0.0.0")
    row.can_show_web_hub_link = False
    ChannelCapabilitiesAdmin(ChannelCapabilities, AdminSite()).save_model(request, row, None, True)
    assert CapabilityChangeLog.objects.order_by("-id").first().changed_by == operator
