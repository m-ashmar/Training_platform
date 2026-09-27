"""The trainer's money screens' reads (screens/15-trainer-money): BACKEND_BUGS B-096 and
B-101, 2026-09-24.

B-096 — the trainer's side of escrow. `GET /api/wallet/balance/` said `held: 0.00` to a
trainer whatever clients had paid into escrow for them, so TrainerEarnings' «محجوز لدى
المنصة · 3 عملاء بانتظار أول برنامج» was the app multiplying the trainer's CURRENT price
by a guess. Every hold here is taken by the real request/approve endpoints and released
by the real first program, refund and reversal.

B-101 — the ledger's filters and a monthly summary. `GET /api/wallet/transactions/`
ignored every parameter and the app summed one page of 100 rows into a six-month chart.
"""
from __future__ import annotations

import datetime
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
RATE = D("13000.000000")      # 1 USD = 13,000 SYP


# ------------------------------------------------------------------------- helpers
def _person(make_user, name, currency, *, user_type="client", **kw):
    from wallet.models import set_currency_for

    user = make_user(name, user_type=user_type, **kw)
    set_currency_for(user, currency, user_type)
    return user


def _fund(user, amount):
    from wallet.models import Wallet

    wallet = Wallet.objects.get(owner=user)
    wallet.balance = D(amount)
    wallet.save(update_fields=["balance"])
    return wallet


def _trainer(make_user, tag, *, currency="SYP", price="12000.00"):
    return _person(make_user, f"twr_t_{tag}", currency, user_type="trainer",
                   trainer_hourly_rate=D(price), first_name="Rami", last_name="Haddad")


def _client_of(make_user, api, trainer, tag, *, currency="SYP", funds="100000.00",
               first_name="Sam"):
    """A client who asked `trainer` and was accepted: month 1 is held in escrow."""
    from users.models import TrainerClientRelation

    client = _person(make_user, f"twr_c_{tag}", currency, first_name=first_name,
                     last_name="Client")
    _fund(client, funds)
    asked = api(client).post("/api/auth/client/request-trainer/",
                             {"trainer_id": trainer.pk}, content_type=JSON)
    assert asked.status_code == 200, asked.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    relation.refresh_from_db()
    assert relation.hold_reference, "the approval did not hold month 1"
    return client, relation


def _first_program(api, trainer, client):
    from routine.models import Routine

    routine = Routine.objects.create(name="Month one", created_by=trainer, days=3)
    answer = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                               {"client_id": client.pk}, content_type=JSON)
    assert answer.status_code == 200, answer.content


def _balance(api, user, **headers):
    response = api(user).get("/api/wallet/balance/", **headers)
    assert response.status_code == 200, response.content
    return response.json()


# =================================================================== B-096: escrow
def test_b096_a_trainer_reads_what_the_platform_holds_for_them(make_user, api, settings):
    """«محجوز لدى المنصة · N عملاء بانتظار أول برنامج»: every held month 1, at the price
    the client paid, net of the platform's 20% as it will be paid, and who it is for."""
    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _trainer(make_user, "held")
    _client_of(make_user, api, trainer, "held1", first_name="Rana")
    _client_of(make_user, api, trainer, "held2", first_name="Samer")

    body = _balance(api, trainer)
    assert body["held_for_trainer"] == "19200.00"          # 2 x (12,000 - 2,400)
    assert body["held_for_trainer_gross"] == "24000.00"
    assert body["awaiting_first_program"] == 2
    items = body["held_for_trainer_items"]
    assert len(items) == 2
    assert {i["client"]["name"] for i in items} == {"Rana Client", "Samer Client"}
    for item in items:
        assert (item["amount"], item["commission"], item["net"], item["currency"]) == \
            ("12000.00", "2400.00", "9600.00", "SYP")
        assert item["commission_rate"] == "0.2"
        assert (item["charged_amount"], item["charged_currency"]) == ("12000.00", "SYP")
        assert item["status"] == "awaiting_first_program"
        assert item["kind"] == "trainer_fee_hold" and item["reason"]
        assert item["month"] == 1 and item["estimated"] is False
        assert item["held_at"] and item["requested_at"]
        assert item["client"]["role"] == "client"
    # Newest hold first, the order total.
    held_at = [i["held_at"] for i in items]
    assert held_at == sorted(held_at, reverse=True)
    # The trainer's own spendable balance is untouched: held is not available.
    assert body["balance"] == "0.00"


def test_b096_each_hold_at_its_own_recorded_price_and_rate(make_user, api, settings):
    """The amount is the month's own price and commission rate, never the trainer's
    current price or today's rate: a trainer who raises their fee, or an operator who
    changes the commission, does not re-value money already held."""
    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _trainer(make_user, "own", price="10000.00")
    _client_of(make_user, api, trainer, "own1")
    trainer.trainer_hourly_rate = D("50000.00")
    trainer.save(update_fields=["trainer_hourly_rate"])
    settings.COACHING_COMMISSION_RATE = D("0.10")
    _client_of(make_user, api, trainer, "own2")          # quoted at 50,000, 10%

    items = {i["amount"]: i for i in _balance(api, trainer)["held_for_trainer_items"]}
    assert set(items) == {"10000.00", "50000.00"}
    assert (items["10000.00"]["commission"], items["10000.00"]["net"],
            items["10000.00"]["commission_rate"]) == ("2000.00", "8000.00", "0.2")
    assert (items["50000.00"]["commission"], items["50000.00"]["net"],
            items["50000.00"]["commission_rate"]) == ("5000.00", "45000.00", "0.1")
    assert _balance(api, trainer)["held_for_trainer"] == "53000.00"


def test_b096_released_refunded_and_reversed_months_are_no_longer_held(
        make_user, api, settings):
    """Only money still in escrow for the trainer counts: the first program releases it
    into the balance, a client who leaves takes it back, and an operator's reversal of
    the hold row sent it back to the client even though the relation still names it."""
    from users.trainer_relations import release_hold
    from wallet.models import Transaction, move_funds_atomic

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _trainer(make_user, "gone")
    paid, _r1 = _client_of(make_user, api, trainer, "gone1")
    _left, left_relation = _client_of(make_user, api, trainer, "gone2")
    _rev, reversed_relation = _client_of(make_user, api, trainer, "gone3")
    _kept, _r4 = _client_of(make_user, api, trainer, "gone4", first_name="Kept")
    assert _balance(api, trainer)["awaiting_first_program"] == 4

    _first_program(api, trainer, paid)                   # released: paid out
    release_hold(left_relation, left_relation.client)    # refunded to the client
    hold = Transaction.objects.get(reference_id=reversed_relation.hold_reference)
    move_funds_atomic(hold.destination_wallet, hold.source_wallet, hold.amount,
                      tx_type="reversal", money_type=hold.money_type,
                      metadata={"original_reference": hold.reference_id}, reverses=hold)
    reversed_relation.refresh_from_db()
    assert reversed_relation.hold_reference, "the reversal is not what clears the relation"

    body = _balance(api, trainer)
    assert body["awaiting_first_program"] == 1
    assert [i["client"]["name"] for i in body["held_for_trainer_items"]] == ["Kept Client"]
    assert body["held_for_trainer"] == "9600.00"
    assert body["balance"] == "9600.00"                  # the released one is spendable


def test_b096_a_month_charged_in_the_other_currency_is_the_trainers_money_estimated(
        make_user, api, settings):
    """A dollar trainer and a pound client: the month is held in pounds and the trainer's
    share is converted when it is paid, so the figure is today's rate and says so. With
    no usable rate it falls back to the trainer's own quote in their currency."""
    from wallet.models import ExchangeRate

    settings.COACHING_COMMISSION_RATE = D("0.20")
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(hours=1),
                                note="gate")
    trainer = _trainer(make_user, "fx", currency="USD", price="10.00")
    _client_of(make_user, api, trainer, "fx1", currency="SYP", funds="1000000.00")

    body = _balance(api, trainer)
    (item,) = body["held_for_trainer_items"]
    assert (item["charged_amount"], item["charged_currency"]) == ("130000.00", "SYP")
    assert (item["amount"], item["commission"], item["net"], item["currency"]) == \
        ("10.00", "2.00", "8.00", "USD")
    assert item["estimated"] is True
    assert (body["held_for_trainer"], body["held_for_trainer_gross"]) == ("8.00", "10.00")

    # The rate goes stale: the trainer's quote of record, still in dollars.
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(days=60),
                                note="gate old")
    ExchangeRate.objects.filter(effective_from__gt=timezone.now() - timedelta(days=2)) \
        .update(effective_from=timezone.now() - timedelta(days=59))
    stale = _balance(api, trainer)
    (item,) = stale["held_for_trainer_items"]
    assert (item["amount"], item["net"], item["currency"], item["estimated"]) == \
        ("10.00", "8.00", "USD", True)
    assert stale["held_for_trainer"] == "8.00"


def test_b096_the_held_months_are_not_written_as_ledger_rows(make_user, api, settings):
    """The ledger is append-only double entry: a hold is ONE row, client -> escrow, and
    none of it is the trainer's until it is released. Reading it writes nothing."""
    from wallet.models import Transaction

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _trainer(make_user, "norows")
    _client_of(make_user, api, trainer, "norows1")
    before = Transaction.objects.count()
    assert _balance(api, trainer)["awaiting_first_program"] == 1
    assert Transaction.objects.count() == before
    ledger = api(trainer).get("/api/wallet/transactions/").json()
    assert ledger["count"] == 0 and ledger["results"] == []


def test_b096_anyone_but_a_trainer_reads_zeroes_and_the_client_keeps_held(
        make_user, api, settings):
    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _trainer(make_user, "zero")
    client, _relation = _client_of(make_user, api, trainer, "zero1")
    body = _balance(api, client)
    assert (body["held_for_trainer"], body["held_for_trainer_gross"],
            body["awaiting_first_program"], body["held_for_trainer_items"]) == \
        ("0.00", "0.00", 0, [])
    assert body["held"] == "12000.00"                    # the client's side, as before
    assert body["held_for"]["trainer_id"] == trainer.pk


def test_b096_the_same_on_every_build_and_bounded_in_queries(make_user, api, settings):
    """Money already paid is shown on every build (spec §3), and the read costs the same
    few queries for one client as for five."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _trainer(make_user, "bound")
    _client_of(make_user, api, trainer, "bound0")
    with CaptureQueriesContext(connection) as one:
        _balance(api, trainer)
    for n in range(1, 5):
        _client_of(make_user, api, trainer, f"bound{n}")
    with CaptureQueriesContext(connection) as five:
        apk = _balance(api, trainer, HTTP_X_APP_CHANNEL="apk")
    assert apk["awaiting_first_program"] == 5
    assert len(five) <= len(one), (len(one), len(five))
    for channel in ("play", "ios", "web", ""):
        assert _balance(api, trainer, HTTP_X_APP_CHANNEL=channel) == apk, channel


# ================================================================= B-101: filters
def _at(day, hour=12, minute=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)),
                               timezone.get_default_timezone())


def _row(source, destination, amount, when, *, money_type="WALLET_MOVE",
         tx_type="transfer", metadata=None, actor=None):
    """One ledger row at `when`. Written straight to the table, as the ledger's own gate
    test does: these tests are about READING rows, and the money functions stamp
    `created_at` with now."""
    from tests.ledger_rows import backdate
    from wallet.models import Transaction

    row = Transaction.objects.create(
        source_wallet=source, destination_wallet=destination, amount=D(amount),
        tx_type=tx_type, money_type=money_type, metadata=metadata or {}, actor=actor,
        currency=(destination or source).currency)
    # Shared setup, changed deliberately: the ledger refuses UPDATE at the database
    # since wallet/0021, so the back-dating goes through the one test-only escape.
    backdate(row.pk, when)
    row.refresh_from_db()
    return row


def _wallets(make_user, tag):
    from wallet.models import wallet_for

    trainer = make_user(f"twr_lt_{tag}", user_type="trainer")
    clients = [make_user(f"twr_lc_{tag}{i}", first_name=f"C{i}") for i in range(3)]
    return (trainer, wallet_for(trainer, "trainer"),
            [(c, wallet_for(c, "client")) for c in clients])


def _refs(response):
    assert response.status_code == 200, response.content
    return [r["reference_id"] for r in response.json()["results"]]


def test_b101_since_and_until_are_inclusive_days_in_the_servers_zone(make_user, api):
    trainer, mine, clients = _wallets(make_user, "days")
    (_c, theirs) = clients[0]
    day = datetime.date(2026, 8, 10)
    before = _row(theirs, mine, "1.00", _at(day - timedelta(days=1), 23, 59))
    first = _row(theirs, mine, "2.00", _at(day, 0, 0))
    last = _row(theirs, mine, "3.00", _at(day + timedelta(days=2), 23, 59))
    after = _row(theirs, mine, "4.00", _at(day + timedelta(days=3), 0, 0))

    got = _refs(api(trainer).get("/api/wallet/transactions/",
                                 {"since": "2026-08-10", "until": "2026-08-12"}))
    assert got == [last.reference_id, first.reference_id]
    assert _refs(api(trainer).get("/api/wallet/transactions/", {"since": "2026-08-13"})) \
        == [after.reference_id]
    assert _refs(api(trainer).get("/api/wallet/transactions/", {"until": "2026-08-09"})) \
        == [before.reference_id]
    # No filter: every row, newest first, exactly as before.
    assert _refs(api(trainer).get("/api/wallet/transactions/")) == \
        [after.reference_id, last.reference_id, first.reference_id, before.reference_id]


def test_b101_money_type_takes_one_or_several(make_user, api):
    trainer, mine, clients = _wallets(make_user, "mt")
    (_c, theirs) = clients[0]
    when = _at(datetime.date(2026, 9, 1))
    coaching = _row(None, mine, "10.00", when, money_type="COACHING_1TO1")
    share = _row(theirs, mine, "5.00", when + timedelta(minutes=1), money_type="PLATFORM_SUB")
    moved = _row(theirs, mine, "1.00", when + timedelta(minutes=2))

    client = api(trainer)
    assert _refs(client.get("/api/wallet/transactions/", {"money_type": "COACHING_1TO1"})) \
        == [coaching.reference_id]
    both = _refs(client.get("/api/wallet/transactions/?money_type=COACHING_1TO1"
                            "&money_type=PLATFORM_SUB"))
    assert both == [share.reference_id, coaching.reference_id]
    assert _refs(client.get("/api/wallet/transactions/",
                            {"money_type": "platform_sub,WALLET_MOVE"})) == \
        [moved.reference_id, share.reference_id]


def test_b101_kind_filters_exactly_as_every_row_is_labelled(make_user, api):
    """`?kind=` is `kind_of` in SQL (`wallet.activity.kind_q`). A row for every branch of
    `kind_of`, including the awkward ones (a known purpose on a reversal, a falsy agent
    id, a purpose that is not a string, an empty metadata), and every kind must select
    exactly the rows `kind_of` gives that kind — and together they partition the ledger."""
    from wallet.activity import KINDS, PURPOSE_KINDS, kind_of, kind_q
    from wallet.models import Transaction
    from wallet.utils import get_escrow_wallet

    trainer, mine, clients = _wallets(make_user, "kinds")
    (_c, theirs) = clients[0]
    escrow = get_escrow_wallet(mine.currency)
    when = _at(datetime.date(2026, 9, 2))
    shapes = []
    for purpose in PURPOSE_KINDS:
        shapes += [(escrow, mine, "transfer", {"purpose": purpose}),
                   (mine, escrow, "transfer", {"purpose": purpose})]
    shapes += [
        (mine, theirs, "reversal", {"original_reference": "x"}),
        (theirs, mine, "reversal", {"purpose": "coaching_payout"}),
        (None, mine, "topup", {"agent_id": 7}),
        (None, mine, "topup", {"agent_id": 0, "original_payment_id": "p-1"}),
        (None, mine, "topup", {"agent_id": None, "original_payment_id": ""}),
        (None, mine, "topup", {"agent_id": "", "original_payment_id": "p-2"}),
        (None, mine, "topup", {}),
        (mine, theirs, "transfer", {"trainer_id": 5}),
        (mine, theirs, "transfer", {"trainer_id": 0}),
        (theirs, mine, "transfer", {"trainer_id": trainer.pk}),
        (theirs, mine, "transfer", {}),
        (mine, theirs, "transfer", {"purpose": "not_a_purpose"}),
        (mine, None, "transfer", {"purpose": 5}),
        (mine, None, "adjustment", {}),
        (None, mine, "adjustment", {"purpose": "something_new"}),
    ]
    rows = [_row(s, d, "1.00", when + timedelta(seconds=i), tx_type=t, metadata=m)
            for i, (s, d, t, m) in enumerate(shapes)]
    ledger = Transaction.objects.filter(pk__in=[r.pk for r in rows])

    seen = set()
    for kind in KINDS:
        expected = {r.pk for r in rows if kind_of(r, mine) == kind}
        got = set(ledger.filter(kind_q(kind, mine)).values_list("pk", flat=True))
        assert got == expected, (kind, sorted(got), sorted(expected))
        assert not (got & seen), kind
        seen |= got
    assert seen == {r.pk for r in rows}, "a row no kind selects"

    # And through the route, one kind and several.
    client = api(trainer)
    payouts = {r.reference_id for r in rows if kind_of(r, mine) == "trainer_fee_payout"}
    assert set(_refs(client.get("/api/wallet/transactions/",
                                {"kind": "trainer_fee_payout", "page_size": 100}))) == payouts
    several = {r.reference_id for r in rows
               if kind_of(r, mine) in ("agent_topup", "transfer_received")}
    assert set(_refs(client.get("/api/wallet/transactions/",
                                {"kind": "agent_topup,transfer_received",
                                 "page_size": 100}))) == several


@pytest.mark.parametrize("path", ["/api/wallet/transactions/",
                                  "/api/wallet/transactions/summary/"])
@pytest.mark.parametrize("params, code", [
    ({"since": "2026-13-01"}, "bad_date"),
    ({"since": "01/09/2026"}, "bad_date"),
    ({"until": "20260901"}, "bad_date"),
    ({"until": "1999-12-31"}, "bad_date"),
    ({"since": "2026-09-10", "until": "2026-09-01"}, "date_range_reversed"),
    ({"money_type": "COACHING"}, "bad_money_type"),
    ({"money_type": "COACHING_1TO1,NOPE"}, "bad_money_type"),
    ({"kind": "salary"}, "bad_kind"),
])
def test_b101_every_refusal_names_its_code(make_user, api, path, params, code):
    user = make_user("twr_ref")
    response = api(user).get(path, params)
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["code"] == code, body
    if code == "bad_date":
        assert body["field"] in ("since", "until")
    if code == "bad_money_type":
        assert set(body["money_type_options"]) == {"PLATFORM_SUB", "COACHING_1TO1",
                                                   "ROUTINE_PRODUCT", "WALLET_MOVE"}
    if code == "bad_kind":
        assert "trainer_fee_payout" in body["kind_options"]


def test_b101_the_summary_window_is_at_most_twelve_months(make_user, api):
    user = make_user("twr_window")
    client = api(user)
    ok = client.get("/api/wallet/transactions/summary/",
                    {"since": "2025-10-15", "until": "2026-09-01"})
    assert ok.status_code == 200 and len(ok.json()["months"]) == 12
    long = client.get("/api/wallet/transactions/summary/",
                      {"since": "2025-09-30", "until": "2026-09-01"})
    assert long.status_code == 400
    assert long.json()["code"] == "date_range_too_long"
    assert long.json()["max_months"] == 12

    default = client.get("/api/wallet/transactions/summary/").json()
    today = timezone.localdate()
    assert default["until"] == today.isoformat()
    months = [m["month"] for m in default["months"]]
    assert len(months) == 6 and months[-1] == today.strftime("%Y-%m")
    assert default["since"] == f"{months[0]}-01"


def test_b101_the_summary_sums_each_month_by_kind_of_money(make_user, api):
    """The chart and «من أين أرباح أيلول»: coaching from two clients, a plan share and a
    program sale in September, one payout in August, money out, and a row outside the
    window. Escrow is nobody: a coaching payout is WITH the client it names."""
    from wallet.utils import get_escrow_wallet

    trainer, mine, clients = _wallets(make_user, "sum")
    (c0, w0), (c1, w1), (c2, w2) = clients
    escrow = get_escrow_wallet(mine.currency)
    sep, aug = datetime.date(2026, 9, 3), datetime.date(2026, 8, 20)
    payout = {"purpose": "coaching_payout", "trainer_id": trainer.pk}
    _row(escrow, mine, "9600.00", _at(sep), money_type="COACHING_1TO1",
         metadata={**payout, "client_id": c0.pk})
    _row(escrow, mine, "9600.00", _at(sep, 13), money_type="COACHING_1TO1",
         metadata={**payout, "client_id": c1.pk})
    _row(escrow, mine, "9600.00", _at(sep, 14), money_type="COACHING_1TO1",
         metadata={**payout, "client_id": c0.pk})          # c0 again: still 2 people
    _row(w2, mine, "500.00", _at(sep, 15), money_type="PLATFORM_SUB",
         metadata={"purpose": "subscription_trainer_share"})
    _row(w1, mine, "3200.00", _at(sep, 16), money_type="ROUTINE_PRODUCT")
    _row(mine, None, "1000.00", _at(sep, 17))                # money out
    _row(escrow, mine, "8000.00", _at(aug), money_type="COACHING_1TO1",
         metadata={**payout, "client_id": c2.pk})
    _row(escrow, mine, "7.00", _at(datetime.date(2026, 4, 30)),
         money_type="COACHING_1TO1", metadata={**payout, "client_id": c2.pk})

    response = api(trainer).get("/api/wallet/transactions/summary/",
                                {"since": "2026-05-01", "until": "2026-09-30"})
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["currency"] == mine.currency
    assert (body["since"], body["until"]) == ("2026-05-01", "2026-09-30")
    assert [m["month"] for m in body["months"]] == \
        ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    may = body["months"][0]
    assert (may["in"], may["out"], may["count"]) == ("0.00", "0.00", 0)
    assert set(may["by_money_type"]) == {"PLATFORM_SUB", "COACHING_1TO1",
                                         "ROUTINE_PRODUCT", "WALLET_MOVE"}

    september = body["months"][-1]
    assert (september["in"], september["out"], september["net"], september["count"]) == \
        ("32500.00", "1000.00", "31500.00", 6)
    coaching = september["by_money_type"]["COACHING_1TO1"]
    assert (coaching["in"], coaching["count"], coaching["people"]) == ("28800.00", 3, 2)
    assert september["by_money_type"]["PLATFORM_SUB"] == \
        {"in": "500.00", "out": "0.00", "net": "500.00", "count": 1, "people": 1}
    sold = september["by_money_type"]["ROUTINE_PRODUCT"]
    assert (sold["in"], sold["count"], sold["people"]) == ("3200.00", 1, 1)
    moved = september["by_money_type"]["WALLET_MOVE"]
    assert (moved["out"], moved["count"], moved["people"]) == ("1000.00", 1, 0)

    august = body["months"][-2]
    assert august["by_money_type"]["COACHING_1TO1"]["in"] == "8000.00"
    totals = body["totals"]
    assert (totals["in"], totals["out"], totals["count"]) == ("40500.00", "1000.00", 7)
    # Three different clients paid for coaching over the window.
    assert totals["by_money_type"]["COACHING_1TO1"]["people"] == 3

    only = api(trainer).get("/api/wallet/transactions/summary/",
                            {"since": "2026-09-01", "until": "2026-09-30",
                             "money_type": "COACHING_1TO1"}).json()
    assert only["months"][0]["in"] == "28800.00" and only["money_type"] == ["COACHING_1TO1"]
    payouts = api(trainer).get("/api/wallet/transactions/summary/",
                               {"since": "2026-09-01", "until": "2026-09-30",
                                "kind": "trainer_fee_payout"}).json()
    assert payouts["months"][0]["count"] == 3


def test_b101_the_summary_costs_the_same_queries_for_many_rows(make_user, api):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    trainer, mine, clients = _wallets(make_user, "cost")
    (_c, theirs) = clients[0]
    client = api(trainer)
    params = {"since": "2026-04-01", "until": "2026-09-30"}
    _row(theirs, mine, "1.00", _at(datetime.date(2026, 9, 1)))
    with CaptureQueriesContext(connection) as few:
        assert client.get("/api/wallet/transactions/summary/", params).status_code == 200
    for i in range(60):
        _row(theirs, mine, "1.00", _at(datetime.date(2026, 4 + i % 6, 1 + i % 27)),
             money_type=["WALLET_MOVE", "COACHING_1TO1", "PLATFORM_SUB"][i % 3])
    with CaptureQueriesContext(connection) as many:
        body = client.get("/api/wallet/transactions/summary/", params).json()
    assert body["totals"]["count"] == 61
    assert len(many) <= len(few), (len(few), len(many))


def test_b101_the_summary_is_the_same_on_every_build(make_user, api):
    trainer, mine, clients = _wallets(make_user, "build")
    (_c, theirs) = clients[0]
    _row(theirs, mine, "5.00", _at(datetime.date(2026, 9, 1)), money_type="PLATFORM_SUB")
    params = {"since": "2026-09-01", "until": "2026-09-30"}
    answer = api(trainer).get("/api/wallet/transactions/summary/", params,
                              HTTP_X_APP_CHANNEL="apk")
    assert answer.status_code == 200, answer.content
    apk = answer.json()
    assert apk["totals"]["by_money_type"]["PLATFORM_SUB"]["in"] == "5.00"
    for channel in ("play", "ios", "web", ""):
        assert api(trainer).get("/api/wallet/transactions/summary/", params,
                                HTTP_X_APP_CHANNEL=channel).json() == apk, channel
