"""Road to 10 R1 (2026-11-24): "No sentence on a store build points at paying somewhere
else" — for the notices the server writes.

A notice is written for the ACCOUNT and was read, and pushed, identically on every build.
Three now depend on the reading build's policy flags (`notifications/build_gate.py`; the
manager's rulings under the owner's delegation):

  checkout_expired      not shown at all where can_purchase_platform_sub is off
  subscription_expired  a store text there, without "needs an active subscription"
  coaching_payment_due  a store text where can_pay_coaching_in_app is off

The inbox chooses by the request's X-App-Channel / X-App-Version; a push by the build
each device token recorded. apk and web read everything exactly as before
(`test_apk_and_web_read_every_money_notice_as_before` passes on the code before too: it
is the proof that nothing changed for them); records of money that moved read the same on
every build. The other tests fail on the code before this change.
"""
import types
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
INBOX = "/api/social/notifications/"
TOKENS = "/api/auth/device-token/"
BUILDS = ("apk", "play", "ios", "web")

#: Every notice that mentions money (the investigation's 21), with a context, and the
#: English it rendered before this change: the "before" every apk and web read is held to.
MONEY = {
    "checkout_expired": ({}, "Your checkout expired",
                         "You did not finish your last checkout, so it was closed. You can "
                         "start again whenever you like."),
    "subscription_expired": ({}, "Your subscription has ended",
                             "Your diet plan and routine are saved and you can still see "
                             "them. Using them needs an active subscription."),
    "coaching_payment_due": ({"name": "Rami"}, "Your coaching is paused",
                             "Your coaching with Rami is paused until the next month is "
                             "paid. You can still see your routines."),
    "subscription_expiring": ({"days": 3}, "Your subscription ends soon",
                              "Your subscription ends in 3 days."),
    "coaching_ended": ({"name": "Rami"}, "Coaching ended",
                       "Coaching between you and Rami has ended: the monthly fee was not "
                       "paid."),
    "coaching_ending_soon": ({"name": "Rami", "days": 3}, "Your coaching ends soon",
                             "Your month of coaching with Rami ends in 3 days, and it will "
                             "not renew. Nothing more will be charged."),
    "coaching_fee_held": ({"name": "Rami"}, "Coaching fee held",
                          "The first month's fee for coaching between you and Rami is "
                          "held. It is paid to the trainer when the first program is sent."),
    "coaching_fee_refunded": ({"name": "Rami"}, "Coaching fee returned",
                              "The held fee for coaching between you and Rami was returned "
                              "to the client's wallet."),
    "coaching_fee_released": ({"name": "Rami", "month": 2}, "Coaching fee paid out",
                              "The fee for month 2 of coaching between you and Rami was "
                              "paid to the trainer."),
    "coaching_renewal_soon": ({"name": "Rami", "days": 3}, "Your coaching renews soon",
                              "Your month of coaching with Rami ends in 3 days."),
    "coaching_renewed": ({"name": "Rami"}, "Coaching renewed",
                         "Your coaching with Rami continues for another month."),
    "payment_confirmed": ({}, "Payment confirmed",
                          "We received your payment. Your receipt is ready in your payment "
                          "history."),
    "payment_needs_review": ({}, "We are checking your payment",
                             "We received a transfer that does not exactly match your "
                             "payment. Our team is checking it and will let you know."),
    "payment_rejected": ({}, "We could not accept your transfer",
                         "The transfer we checked could not be matched to your payment. "
                         "Contact support to have it returned."),
    "routine_product_purchased": ({"title": "Strong"}, "Your program is ready",
                                  "Strong is in your routines now."),
    "routine_product_refunded": ({"title": "Strong"}, "Your program purchase was refunded",
                                 "Strong was refunded to your wallet and removed from your "
                                 "routines."),
    "routine_product_sale_refunded": ({"title": "Strong"}, "A program sale was refunded",
                                      "A purchase of Strong was refunded, and your share "
                                      "of it returned."),
    "routine_product_sold": ({"title": "Strong"}, "You sold a program",
                             "Someone bought Strong."),
    "wallet_topped_up": ({"agent": "Cash shop"}, "Your balance was topped up",
                         "A cash top-up from Cash shop was added to your balance."),
    "withdrawal_failed": ({"number": "W-1", "reason": "wrong address"},
                          "Your withdrawal could not be completed",
                          "Withdrawal W-1 failed and the money is back in your balance: "
                          "wrong address"),
    "withdrawal_paid": ({"number": "W-1"}, "Your withdrawal arrived",
                        "Withdrawal W-1 was sent and has arrived."),
    "withdrawal_requested": ({"number": "W-1"}, "Withdrawal requested",
                             "We received your withdrawal request W-1. We process requests "
                             "every day before noon."),
}
GATED = ("checkout_expired", "subscription_expired", "coaching_payment_due")
RECORDS = tuple(sorted(set(MONEY) - set(GATED)))

STORE_SUBSCRIPTION = "Your diet plan and routine are saved and you can still see them."
STORE_COACHING = "Your coaching with Rami is paused. You can still see your routines."


def _name(tag):
    return f"r1n_{tag}_{uuid.uuid4().hex[:6]}"


def _data(event_type):
    data = {"type": event_type, "ref": "r-1"}
    if event_type == "wallet_topped_up":
        data.update(amount="5000.00", currency="SYP")
    return data


def _write(person, event_types, capture):
    from notifications.services import NotificationService

    with capture(execute=True):
        for event_type in event_types:
            NotificationService.create_and_send(
                recipient=person, event_type=event_type,
                related_object_id=uuid.uuid4().hex,
                metadata={"context": MONEY[event_type][0], "data": _data(event_type)})


def _headers(build, lang="en"):
    # `""` is a request that declares no build: the test client otherwise speaks as the
    # direct APK (tests/conftest.py `speaks_as_the_direct_apk`).
    headers = {"HTTP_ACCEPT_LANGUAGE": lang, "HTTP_X_APP_CHANNEL": build or ""}
    if build:
        headers["HTTP_X_APP_VERSION"] = "1.0.0"
    return headers


def _inbox(api, person, build, lang="en"):
    """{type: (title, message, data)} and the unread count, as this build reads them."""
    body = api(person).get(f"{INBOX}?page_size=100", **_headers(build, lang)).json()
    rows = body["results"] if isinstance(body, dict) else body
    listed = {r["notification_type"]: (r["title"], r["message"], r["data"]) for r in rows}
    unread = api(person).get(f"{INBOX}unread_count/",
                             **_headers(build, lang)).json()["unread_count"]
    return listed, unread, rows


@pytest.fixture
def pushes(monkeypatch):
    """Every multicast FCM is handed: {token_tag: (title, body, data)}."""
    from firebase_admin import messaging

    sent = []

    def send_each_for_multicast(message, dry_run=False):
        for token in message.tokens:
            sent.append((token, message.notification.title, message.notification.body,
                         dict(message.data)))
        return types.SimpleNamespace(
            responses=[types.SimpleNamespace(success=True, exception=None)
                       for _ in message.tokens],
            success_count=len(message.tokens), failure_count=0)

    monkeypatch.setattr(messaging, "send_each_for_multicast", send_each_for_multicast)
    return sent


def _register(api, person, tag, build=None, platform="android"):
    headers = ({"HTTP_X_APP_CHANNEL": build, "HTTP_X_APP_VERSION": "1.0.0"} if build
               else {"HTTP_X_APP_CHANNEL": ""})
    token = f"tok-{tag}-{uuid.uuid4().hex}"
    resp = api(person).post(TOKENS, {"token": token, "platform": platform},
                            content_type=JSON, **headers)
    assert resp.status_code in (200, 201), resp.content
    return token


def _received(sent, token):
    return [(title, body) for tok, title, body, _data in sent if tok == token]


# ------------------------------------------------------------------ apk and web: as before
def test_apk_and_web_read_every_money_notice_as_before(make_user, api,
                                                       django_capture_on_commit_callbacks):
    """Text, data and unread count, for all 22 money notices, on apk and on web: exactly
    what they rendered before this change (this test passes on the code before it)."""
    person = make_user(_name("before"))
    _write(person, MONEY, django_capture_on_commit_callbacks)
    for build in ("apk", "web"):
        listed, unread, _rows = _inbox(api, person, build)
        assert unread == len(MONEY), build
        assert set(listed) == set(MONEY), build
        for event_type, (_context, title, body) in MONEY.items():
            assert listed[event_type] == (title, body, _data(event_type)), (build,
                                                                            event_type)


def test_apk_and_web_devices_are_pushed_every_money_notice_as_before(
        make_user, api, pushes, django_capture_on_commit_callbacks):
    person = make_user(_name("pushbefore"))
    apk = _register(api, person, "apk", "apk")
    web = _register(api, person, "web", "web", platform="web")
    _write(person, MONEY, django_capture_on_commit_callbacks)
    for token in (apk, web):
        got = _received(pushes, token)
        assert sorted(got) == sorted((title, body) for _c, title, body in MONEY.values())


# ------------------------------------------------------------------ play and ios: the inbox
def test_play_and_ios_read_the_three_gated_notices_by_build(make_user, api,
                                                            django_capture_on_commit_callbacks):
    person = make_user(_name("store"))
    _write(person, GATED, django_capture_on_commit_callbacks)

    play, play_unread, _ = _inbox(api, person, "play")
    assert "checkout_expired" not in play
    assert play["subscription_expired"][:2] == ("Your subscription has ended",
                                                STORE_SUBSCRIPTION)
    # play may pay for coaching in the app: the default text.
    assert play["coaching_payment_due"][:2] == MONEY["coaching_payment_due"][1:]
    assert play_unread == 2

    ios, ios_unread, _ = _inbox(api, person, "ios")
    assert "checkout_expired" not in ios
    assert ios["subscription_expired"][1] == STORE_SUBSCRIPTION
    assert ios["coaching_payment_due"][:2] == ("Your coaching is paused", STORE_COACHING)
    assert ios_unread == 2
    # `data` never changes with the build.
    assert ios["coaching_payment_due"][2] == _data("coaching_payment_due")


def test_the_store_texts_in_arabic(make_user, api, django_capture_on_commit_callbacks):
    person = make_user(_name("ar"), preferred_language="ar")
    _write(person, GATED, django_capture_on_commit_callbacks)
    ios, _unread, _ = _inbox(api, person, "ios", lang="ar")
    assert ios["subscription_expired"][1] == ("خطتك الغذائية وبرنامجك التدريبي محفوظان "
                                              "ويمكنك الاطلاع عليهما.")
    assert ios["coaching_payment_due"][1] == ("تدريبك مع Rami متوقف مؤقتاً. لا يزال "
                                              "بإمكانك الاطلاع على برامجك.")


def test_a_hidden_notice_does_not_open_by_id_on_that_build(make_user, api,
                                                           django_capture_on_commit_callbacks):
    from notifications.models import Notification

    person = make_user(_name("byid"))
    _write(person, ["checkout_expired"], django_capture_on_commit_callbacks)
    row = Notification.objects.get(recipient=person, event_type="checkout_expired")
    assert api(person).get(f"{INBOX}{row.pk}/", **_headers("ios")).status_code == 404
    assert api(person).get(f"{INBOX}{row.pk}/", **_headers("apk")).status_code == 200


# ------------------------------------------------------------------ play and ios: the push
def test_each_device_is_pushed_what_its_build_is_shown(make_user, api, pushes,
                                                      django_capture_on_commit_callbacks):
    person = make_user(_name("devices"))
    tokens = {build: _register(api, person, build, build,
                               platform="ios" if build == "ios" else "android")
              for build in BUILDS}
    _write(person, GATED, django_capture_on_commit_callbacks)

    def bodies(build):
        return {body for _title, body in _received(pushes, tokens[build])}

    default = {MONEY[e][2] for e in GATED}
    assert bodies("apk") == default and bodies("web") == default
    assert bodies("play") == {STORE_SUBSCRIPTION, MONEY["coaching_payment_due"][2]}
    assert bodies("ios") == {STORE_SUBSCRIPTION, STORE_COACHING}


def test_a_token_whose_build_is_unknown_gets_the_strict_form(make_user, api, pushes,
                                                            django_capture_on_commit_callbacks):
    """Registered with no X-App-Channel (every token from before this change): the
    strictest build, as policy answers a missing channel; `platform: ios` is the ios
    build. All its devices withheld: settled `withheld`, never retried."""
    from notifications.models import Notification

    person = make_user(_name("unknown"))
    android = _register(api, person, "old-android")
    iphone = _register(api, person, "old-iphone", platform="ios")
    _write(person, GATED, django_capture_on_commit_callbacks)
    for token in (android, iphone):
        assert {body for _t, body in _received(pushes, token)} == {STORE_SUBSCRIPTION,
                                                                   STORE_COACHING}
    status = Notification.objects.get(recipient=person,
                                      event_type="checkout_expired").status["fcm"]
    assert status["status"] == "withheld" and not status["sent_tokens"]
    assert set(status["withheld_tokens"]) == {android, iphone}


def test_the_token_route_records_the_build(make_user, api):
    from users.models import DeviceToken

    person = make_user(_name("reg"))
    token = _register(api, person, "rec", "play")
    row = DeviceToken.objects.get(token=token)
    assert (row.app_channel, row.app_version) == ("play", "1.0.0")
    # Re-registered without a channel: the recorded build stays.
    assert api(person).post(TOKENS, {"token": token}, content_type=JSON,
                            HTTP_X_APP_CHANNEL="").status_code == 200
    row.refresh_from_db()
    assert row.app_channel == "play"
    unknown = DeviceToken.objects.get(token=_register(api, person, "none"))
    assert unknown.app_channel is None


# ------------------------------------------------------------------ rows and counts
def test_a_row_written_before_the_change_is_read_by_build(make_user, api):
    """The row as the code before wrote it (context and data only): the text is chosen
    when it is READ, so an old row follows the reading build too."""
    from notifications.models import Notification

    person = make_user(_name("old"))
    for event_type in GATED:
        Notification.objects.create(
            recipient=person, event_type=event_type, deduplication_key=uuid.uuid4().hex,
            metadata={"context": MONEY[event_type][0], "data": _data(event_type)})
    ios, unread, _ = _inbox(api, person, "ios")
    assert set(ios) == {"subscription_expired", "coaching_payment_due"} and unread == 2
    apk, unread, _ = _inbox(api, person, "apk")
    assert set(apk) == set(GATED) and unread == 3


@pytest.mark.parametrize("build", (*BUILDS, None))
def test_the_unread_count_agrees_with_the_list_on_every_build(
        make_user, api, build, django_capture_on_commit_callbacks):
    person = make_user(_name(f"count_{build}"))
    _write(person, MONEY, django_capture_on_commit_callbacks)
    _listed, unread, rows = _inbox(api, person, build)
    assert unread == sum(1 for row in rows if not row["is_read"])
    api(person).post(f"{INBOX}mark_all_read/", **_headers(build))
    assert _inbox(api, person, build)[1] == 0
    if build is None:
        # No channel declared: the strictest build, all three gated.
        assert "checkout_expired" not in {r["notification_type"] for r in rows}


def test_every_record_and_state_notice_reads_the_same_on_every_build(
        make_user, api, django_capture_on_commit_callbacks):
    """A receipt, a fee held or paid out, a top-up with its amount, a sale, a withdrawal,
    and a reminder of state: never gated."""
    from notifications.build_gate import gated_event_types

    assert set(gated_event_types()) == set(GATED)
    person = make_user(_name("records"))
    _write(person, RECORDS, django_capture_on_commit_callbacks)
    seen = {build: _inbox(api, person, build)[0] for build in (*BUILDS, None)}
    for build, listed in seen.items():
        for event_type in RECORDS:
            assert listed[event_type] == (MONEY[event_type][1], MONEY[event_type][2],
                                          _data(event_type)), (build, event_type)


@pytest.mark.parametrize("build", ("apk", "ios", None))
def test_every_inbox_route_reads_the_builds_flags_once_per_request(make_user, api, build):
    """The build's capability rows (`policy.services.flags_for`) are read ONCE per
    request on each inbox route (`NotificationViewSet._build_flags`), however many rows
    the page holds or the route touches, and not at all when no channel is declared (the
    strictest build needs no row). That one read is the inbox budget's seventh query
    (tests/test_regression_gate.py); without cachalot, so every read reaches the
    database and is counted."""
    from cachalot.api import cachalot_disabled
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from notifications.models import Notification

    person = make_user(_name(f"once_{build}"))
    rows = [Notification.objects.create(
        recipient=person, event_type="custom", deduplication_key=uuid.uuid4().hex,
        metadata={"context": {"message": f"m{i}"}}) for i in range(25)]
    routes = (("get", f"{INBOX}?page_size=20"), ("get", f"{INBOX}unread_count/"),
              ("get", f"{INBOX}{rows[0].pk}/"), ("post", f"{INBOX}{rows[1].pk}/mark_read/"),
              ("post", f"{INBOX}mark_all_read/"))
    for method, path in routes:
        with cachalot_disabled(), CaptureQueriesContext(connection) as seen:
            answered = getattr(api(person), method)(path, **_headers(build))
        assert answered.status_code == 200, (path, answered.content)
        reads = sum("policy_channelcapabilities" in q["sql"] for q in seen.captured_queries)
        assert reads == (1 if build else 0), (method, path, reads)


def test_the_gate_reads_the_build_and_nothing_else():
    """`choose` and `hidden_event_types` take flags; the flags come from
    `policy.services.flags_for(channel, version)`, which takes no user."""
    import inspect

    from notifications import build_gate
    from policy.services import flags_for

    assert list(inspect.signature(build_gate.choose).parameters) == ["event_type", "flags"]
    assert list(inspect.signature(flags_for).parameters) == ["channel", "version"]
    assert build_gate.hidden_event_types(flags_for("apk")) == []
    assert build_gate.hidden_event_types(flags_for("ios")) == ["checkout_expired"]
    assert build_gate.hidden_event_types(flags_for(None)) == ["checkout_expired"]


def test_registering_the_same_token_again_updates_its_build(make_user, api):
    """The app registers its token on every cold start and token refresh: the same token
    with another channel or version updates the one row, never a second."""
    from users.models import DeviceToken

    person = make_user(_name("again"))
    token = f"tok-again-{uuid.uuid4().hex}"

    def register(channel, version):
        return api(person).post(TOKENS, {"token": token, "platform": "android"},
                                content_type=JSON, HTTP_X_APP_CHANNEL=channel,
                                HTTP_X_APP_VERSION=version)

    assert register("play", "1.0.0").status_code == 201
    assert register("apk", "1.2.0").status_code == 200
    rows = DeviceToken.objects.filter(token=token)
    assert rows.count() == 1
    assert (rows[0].app_channel, rows[0].app_version) == ("apk", "1.2.0")
    assert register("play", "1.3.0").status_code == 200
    assert (DeviceToken.objects.get(token=token).app_channel,
            DeviceToken.objects.get(token=token).app_version) == ("play", "1.3.0")
