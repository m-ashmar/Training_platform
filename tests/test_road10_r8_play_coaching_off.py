"""Road to 10 R8: if Google Play objects to coaching, ONE row turns it off, no release.

The row is the `play` row of the server's table (`policy.ChannelCapabilities`), edited
in the operator console with `can_pay_coaching_in_app` and `can_show_coaching_prices`
unticked (DEPLOY_CHECKLIST.md section 13, on EVERY `play` row). With it off:

- no coaching payment can START from a `play` build: a paid request, accepting a paid
  invitation, `renew: true` (S2.7) and a transfer to a coach all answer 403
  `not_available_on_channel`, and no money moves;
- every read a `play` build makes carries no coaching price;
- nothing changes on `apk`, `web` or `ios`.

What it deliberately leaves running (the manager's ruling, 2026-11-23, options 1A and
2A: the status quo, no money-flow change; stricter is the owner's question): a paid
request already SENT can still be approved by the trainer, from any build, and a
coaching already running keeps renewing from the wallet until the client sends
`renew: false`, which every build accepts. The hourly renewal has no request and so
no build; nothing in it may depend on anything else.

Nothing here is built for R8: it proves that the table already does it.
"""
from decimal import Decimal

import pytest
from django.test import Client

from tests.test_coaching_months import (  # noqa: F401  (owners_rate is an autouse fixture)
    _accepted, _balance, _deliver_first_program, _end_month, _fund, owners_rate,
)

pytestmark = pytest.mark.django_db

JSON = "application/json"
PLAY = {"HTTP_X_APP_CHANNEL": "play"}
RATE = "250.00"
#: The keys a coaching price travels under, found by reading every route below on `apk`.
PRICE_KEYS = {"coaching_price", "trainer_hourly_rate", "agreed_rate", "held_amount"}
FLAGS_OFF = ("can_pay_coaching_in_app", "can_show_coaching_prices")


@pytest.fixture(autouse=True)
def base_rows(db):
    """The rows the seed migration writes (a thread test may have flushed them)."""
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities

    for channel, flags in DEFAULT_FLAGS.items():
        ChannelCapabilities.objects.get_or_create(
            channel=channel, min_version="0.0.0", defaults=flags)


def _console(make_user):
    console = Client()
    console.force_login(make_user("r8_operator", user_type="admin"))
    return console


def _turn_off_coaching_on_play(console, row=None):
    """The operator's step, through the console's change form: untick the two boxes on
    the `play` row and save. Every other box keeps what the row has."""
    from policy.models import FLAG_FIELDS, ChannelCapabilities

    row = row or ChannelCapabilities.objects.get(channel="play", min_version="0.0.0")
    form = {"channel": row.channel, "min_version": row.min_version, "_save": "Save"}
    for name in FLAG_FIELDS:
        if getattr(row, name) and name not in FLAGS_OFF:
            form[name] = "on"
    page = f"/dj-admin/policy/channelcapabilities/{row.pk}/change/"
    assert console.get(page).status_code == 200
    saved = console.post(page, form)
    assert saved.status_code == 302, saved.content.decode()[:2000]
    row.refresh_from_db()
    assert not row.can_pay_coaching_in_app and not row.can_show_coaching_prices
    return row


def _refused(resp):
    assert resp.status_code == 403, resp.content
    assert resp.json()["code"] == "not_available_on_channel", resp.content


def _walk(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _prices(body):
    """Every coaching price in a body: a value under a price key, or the rate itself."""
    found = [(k, v) for k, v in _walk(body) if k in PRICE_KEYS and v not in (None, "")]
    found += [(k, v) for k, v in _walk(body)
              if not isinstance(v, (dict, list)) and str(v) in (RATE, "250", "250.0")]
    return found


def _world(make_user, api):
    """Before the row changes, on the direct APK: a trainer at 250 a month with a client
    coached and past the first programme, a client whose paid request is waiting, a
    client invited at the trainer's price, and a client with money and no coach."""
    trainer, coached, relation = _accepted(make_user, api, "r8", rate=RATE, funds="5000.00")
    _deliver_first_program(api, trainer, coached)
    waiting = make_user("r8_waiting")
    _fund(waiting, "5000.00")
    assert api(waiting).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                             content_type=JSON).status_code == 200
    invited = make_user("r8_invited")
    _fund(invited, "5000.00")
    assert api(trainer).post("/api/auth/trainer/assign-client/", {"client_id": invited.pk},
                             content_type=JSON).status_code == 200
    newcomer = make_user("r8_newcomer")
    _fund(newcomer, "5000.00")
    return trainer, coached, relation, waiting, invited, newcomer


# ------------------------------------------------------------ what the row turns off
def test_no_coaching_payment_can_start_from_a_play_build(make_user, api):
    from users.models import TrainerClientRelation

    trainer, coached, relation, _waiting, invited, newcomer = _world(make_user, api)
    _turn_off_coaching_on_play(_console(make_user))

    # A paid request: refused, nothing written, nothing held.
    _refused(api(newcomer).post("/api/auth/client/request-trainer/",
                                {"trainer_id": trainer.pk}, content_type=JSON, **PLAY))
    assert not TrainerClientRelation.objects.filter(trainer=trainer, client=newcomer).exists()
    assert _balance(newcomer) == Decimal("5000.00")

    # Accepting a paid invitation: refused, still waiting, nothing held.
    invitation = TrainerClientRelation.objects.get(trainer=trainer, client=invited)
    _refused(api(invited).post("/api/auth/client/respond-to-invite/",
                               {"request_id": invitation.pk, "action": "approve"},
                               content_type=JSON, **PLAY))
    invitation.refresh_from_db()
    assert invitation.status == "pending"
    assert _balance(invited) == Decimal("5000.00")

    # S2.7: stopping is accepted on every build; asking to renew again is a payment.
    stop = api(coached).post("/api/v1/coaching/renewal/", {"renew": False},
                             content_type=JSON, **PLAY)
    assert stop.status_code == 200, stop.content
    _refused(api(coached).post("/api/v1/coaching/renewal/", {"renew": True},
                               content_type=JSON, **PLAY))
    relation.refresh_from_db()
    assert relation.client_stops_renewal is True

    # A transfer to a coach: refused, nothing moved.
    before = _balance(coached)
    _refused(api(coached).post("/api/wallet/client/transfer/",
                               {"trainer_id": trainer.pk, "amount": "100.00",
                                "idempotency_key": "r8-transfer"},
                               content_type=JSON, **PLAY))
    assert _balance(coached) == before

    # The same request on the direct APK still goes through: nothing changed there.
    sent = api(newcomer).post("/api/auth/client/request-trainer/",
                              {"trainer_id": trainer.pk}, content_type=JSON)
    assert sent.status_code == 200, sent.content


def test_every_read_a_play_build_makes_hides_the_coaching_price(make_user, api):
    trainer, coached, _relation, waiting, invited, newcomer = _world(make_user, api)
    reads = [
        (newcomer, "/api/v1/trainers/"), (newcomer, f"/api/v1/trainers/{trainer.pk}/"),
        (newcomer, "/api/v1/trainers/matches/"), (newcomer, "/api/auth/trainers/public/"),
        (newcomer, "/api/auth/client/available-trainers/"), (newcomer, "/api/v1/home"),
        (waiting, "/api/auth/client/request-status/"),
        (invited, "/api/auth/client/trainer-invites/"),
        (trainer, "/api/auth/trainer/pending-requests/"),
        (coached, "/api/v1/coaching/current/"), (trainer, "/api/v1/trainer/attention/"),
    ]
    # On `play` before the change, the price is there (coaching is sold on Play today):
    # the scan below can see one.
    shown = {url for who, url in reads if _prices(api(who).get(url, **PLAY).json())}
    assert {"/api/v1/trainers/", "/api/v1/coaching/current/",
            "/api/auth/client/trainer-invites/"} <= shown, shown

    _turn_off_coaching_on_play(_console(make_user))
    for who, url in reads:
        resp = api(who).get(url, **PLAY)
        assert resp.status_code == 200, (url, resp.content)
        assert _prices(resp.json()) == [], (url, _prices(resp.json()))
    caps = api(None).get("/api/v1/config/capabilities/", **PLAY).json()
    flags = caps.get("flags", caps)
    assert flags["can_pay_coaching_in_app"] is False
    assert flags["can_show_coaching_prices"] is False
    # And the APK still shows it.
    assert _prices(api(coached).get("/api/v1/coaching/current/").json())


def test_nothing_changes_on_apk_web_or_ios(make_user, api):
    from policy.services import flags_for

    before = {channel: flags_for(channel, (9, 9, 9)) for channel in ("apk", "web", "ios")}
    caps_before = {channel: api(None).get("/api/v1/config/capabilities/",
                                          HTTP_X_APP_CHANNEL=channel).json()
                   for channel in ("apk", "web", "ios")}
    _turn_off_coaching_on_play(_console(make_user))
    assert {channel: flags_for(channel, (9, 9, 9)) for channel in before} == before
    assert {channel: api(None).get("/api/v1/config/capabilities/",
                                   HTTP_X_APP_CHANNEL=channel).json()
            for channel in caps_before} == caps_before
    # Play itself loses exactly the two flags, and keeps the wallet.
    play = flags_for("play", (9, 9, 9))
    assert play["can_pay_coaching_in_app"] is False and play["can_show_coaching_prices"] is False
    assert play["can_view_wallet"] is True and play["can_transfer_wallet"] is True


# ------------------------------------------------- what it deliberately leaves running
def test_a_request_already_sent_can_still_be_approved_and_a_running_coaching_renews(
        make_user, api):
    """The limits of the row (1A and 2A, the status quo): it stops what a `play` build
    STARTS, not what was agreed before."""
    from unittest import mock

    from users.coaching import renew
    from users.models import TrainerClientRelation

    trainer, coached, relation, waiting, _invited, _newcomer = _world(make_user, api)
    _turn_off_coaching_on_play(_console(make_user))

    # The trainer approves the request sent before, from the play build: month one held.
    request = TrainerClientRelation.objects.get(trainer=trainer, client=waiting)
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": request.pk, "action": "approve"},
                                 content_type=JSON, **PLAY)
    assert answered.status_code == 200, answered.content
    assert _balance(waiting) == Decimal("4750.00")

    # The running coaching renews from the wallet at its month's end.
    before = _balance(coached)
    _end_month(relation)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "renewed"
    assert _balance(coached) == before - Decimal(RATE)

    # Until the client stops it, from the play build too; then it ends, uncharged.
    stop = api(coached).post("/api/v1/coaching/renewal/", {"renew": False},
                             content_type=JSON, **PLAY)
    assert stop.status_code == 200, stop.content
    relation.refresh_from_db()
    _end_month(relation)
    after_stop = _balance(coached)
    with mock.patch("notifications.domain.dispatcher.emit_event"):
        assert renew(relation.pk) == "ended"
    assert _balance(coached) == after_stop


def test_every_play_row_is_the_step_not_only_the_base_one(make_user, api):
    """The table answers a build with the row of the highest `min_version` it has
    reached. With a newer `play` row in place, unticking the base row leaves the builds
    at or above that version selling coaching: the operator unticks EVERY `play` row."""
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities

    trainer, *_rest = _world(make_user, api)
    newer = ChannelCapabilities(channel="play", min_version="2.0.0", **DEFAULT_FLAGS["play"])
    newer.save()
    newcomer = make_user("r8_row_newcomer")
    _fund(newcomer, "5000.00")
    play_2 = {**PLAY, "HTTP_X_APP_VERSION": "2.1.0"}
    console = _console(make_user)

    _turn_off_coaching_on_play(console)
    still = api(newcomer).post("/api/auth/client/request-trainer/",
                               {"trainer_id": trainer.pk}, content_type=JSON, **play_2)
    assert still.status_code == 200, still.content
    # A request holds no money until it is approved; drop it to ask again.
    from users.models import TrainerClientRelation
    TrainerClientRelation.objects.filter(trainer=trainer, client=newcomer).delete()

    _turn_off_coaching_on_play(console, newer)
    _refused(api(newcomer).post("/api/auth/client/request-trainer/",
                                {"trainer_id": trainer.pk}, content_type=JSON, **play_2))


def test_the_console_records_who_turned_it_off(make_user, api):
    from policy.models import CapabilityChangeLog

    _turn_off_coaching_on_play(_console(make_user))
    entry = CapabilityChangeLog.objects.filter(channel="play").order_by("-id").first()
    assert entry.changed_by.username == "r8_operator"
    assert entry.old_values["can_pay_coaching_in_app"] is True
    assert entry.new_values["can_pay_coaching_in_app"] is False
    assert entry.new_values["can_show_coaching_prices"] is False
