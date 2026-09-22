"""Two currencies, both real, and the operator's rate between them.

The owner, 2026-09-21: Yalla Gym trades in Syrian pounds AND US dollars, and in nothing
else; a person picks one and is held to it; every service is buyable in either, with
the charge converted at a rate an operator sets in the console.

These are JOURNEYS, not unit pokes. The point is not that a helper returns a wallet —
it is that a customer can buy in either currency, that the platform ends up holding the
money, that a refund gives back exactly what was taken, and that nothing anywhere
converts at 1:1 or at yesterday's rate.

What each section proves:

| Section | The defect it stands against |
|---|---|
| A person's currency | a chosen currency silently flipped by the first money in |
| A plan in each currency | SIDE_BUGS S-001: ONE platform wallet, so revenue in the second currency sat in customers' balances |
| Refunds | a refund reversing at today's rate, so the platform gains or loses from the clock |
| Coaching | escrow had S-001's shape too, so a month in the second currency could not be held at all |
| Conversion | a rate applied twice, not snapshotted, or guessed when nobody had set one |
| The migration | the platform's existing balance moved or relabelled |
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
#: 1 USD = 13,000 SYP. A round number so every expected figure below can be read.
RATE = D("13000.000000")


# --------------------------------------------------------------------- helpers
@pytest.fixture
def rate(db):
    """A rate an operator set just now, which is what the console writes."""
    from wallet.models import ExchangeRate

    return ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                       effective_from=timezone.now() - timedelta(hours=1),
                                       note="gate")


def _person(make_user, name, currency, *, user_type="client", **kw):
    """A person who has chosen their currency, before any money has moved."""
    from wallet.models import currency_for, set_currency_for

    user = make_user(name, user_type=user_type, **kw)
    set_currency_for(user, currency, user_type)
    assert currency_for(user) == currency
    return user


def _fund(user, amount, currency):
    from wallet.models import Wallet

    wallet = Wallet.objects.get(owner=user)
    assert wallet.currency == currency, (wallet.currency, currency)
    wallet.balance = D(amount)
    wallet.save(update_fields=["balance"])
    return wallet


def _balance(user):
    from wallet.models import Wallet

    return Wallet.objects.get(owner=user).balance


def _platform(currency):
    from wallet.utils import get_platform_revenue_wallet

    return get_platform_revenue_wallet(currency).balance


def _escrow(currency):
    from wallet.utils import get_escrow_wallet

    return get_escrow_wallet(currency).balance


def _plan(tag, price, currency):
    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        return SubscriptionPlan.objects.create(
            name=f"two-cur {tag}", plan_type="premium", description="x",
            price=D(price), currency=currency, duration_days=30, is_active=True)


def _subscription(user, plan):
    from subscription.models import Subscription

    return Subscription.objects.create(
        user=user, plan=plan, status="pending", start_date=timezone.now(),
        end_date=timezone.now() + timedelta(days=30))


def _pay(sub, capture, *, tag):
    """Open a checkout the way the Pay button does, then let the gateway confirm it.

    Through `PaymentService` both times, because it is the only code allowed to move a
    payment's state — and because `start_payment` is where the price is converted into
    the buyer's currency.
    """
    from subscription.services.payment_service import PaymentService

    payment = PaymentService.start_payment(
        sub, lambda p: ("manual", f"ref-{tag}", {}))
    with capture(execute=True):
        PaymentService.complete_payment(payment.id, {
            "amount": str(payment.amount), "currency": payment.currency,
            "status": "completed", "transaction_id": f"tx-{tag}"})
    payment.refresh_from_db()
    return payment


# ============================================================ a person's currency
def test_a_person_picks_a_currency_and_keeps_it_once_money_has_moved(make_user):
    """The owner: "the user can select which currency he wants to pay then we force him
    to it". The wallet IS that choice — one field, not a second one beside it that
    could disagree — so the rule has to be the wallet's: free while it is untouched, a
    fact the moment a balance or a single ledger leg exists.

    Before `currency_chosen_at` a chosen-but-empty wallet was still flipped by
    `wallet_for` to whatever arrived first, silently and at 1:1, which would have paid
    a trainer who asked for dollars in pounds on their very first payout.
    """
    from wallet.models import CurrencyLocked, Wallet, currency_for, set_currency_for

    person = _person(make_user, "cur-pick", "USD")
    # Still free: no balance, no legs.
    set_currency_for(person, "SYP")
    assert currency_for(person) == "SYP"
    set_currency_for(person, "USD")

    # Money arriving in the OTHER currency does not silently re-label a chosen wallet.
    from wallet.models import wallet_for

    assert wallet_for(person, "client", "SYP").currency == "USD"

    _fund(person, "50.00", "USD")
    with pytest.raises(CurrencyLocked) as caught:
        set_currency_for(person, "SYP")
    assert caught.value.code == "currency_locked" and caught.value.currency == "USD"
    assert Wallet.objects.get(owner=person).currency == "USD"


def test_the_app_reads_and_sets_a_persons_currency_on_one_endpoint(make_user, api):
    """`GET`/`PUT /api/wallet/currency/`. The mobile session needs exactly one place
    for this, and `GET /api/wallet/balance/` answers the same three keys."""
    person = make_user("cur-endpoint")
    client = api(person)

    read = client.get("/api/wallet/currency/")
    assert read.status_code == 200, read.content
    assert set(read.json()) == {"currency", "currency_locked", "currency_options"}
    assert read.json()["currency_locked"] is False
    assert sorted(read.json()["currency_options"]) == ["SYP", "USD"]

    put = client.put("/api/wallet/currency/", {"currency": "USD"}, content_type=JSON)
    assert put.status_code == 200 and put.json()["currency"] == "USD"
    assert client.get("/api/wallet/balance/").json()["currency"] == "USD"

    bad = client.put("/api/wallet/currency/", {"currency": "EUR"}, content_type=JSON)
    assert bad.status_code == 400 and bad.json()["code"] == "currency_unsupported"

    _fund(person, "10.00", "USD")
    locked = client.put("/api/wallet/currency/", {"currency": "SYP"}, content_type=JSON)
    assert locked.status_code == 409 and locked.json()["code"] == "currency_locked"
    assert client.get("/api/wallet/currency/").json()["currency_locked"] is True


# ====================================================== a plan in each currency
@pytest.mark.parametrize("currency,price", [("SYP", "5000.00"), ("USD", "10.00")])
def test_a_plan_bought_in_each_currency_lands_in_the_platforms_wallet_for_it(
        make_user, django_capture_on_commit_callbacks, currency, price):
    """SIDE_BUGS S-001, end to end and twice.

    `get_platform_revenue_wallet()` returned ONE wallet, so the first payment ever
    taken decided the platform's currency for ever: a plan paid in the other one was
    minted into the CLIENT's wallet, logged as an error and never moved on — the
    platform's largest revenue line sitting in a customer's balance. There is now one
    ledger wallet per currency, resolved by the money being moved.
    """
    from wallet.models import Transaction

    buyer = _person(make_user, f"plan-{currency.lower()}", currency)
    plan = _plan(currency.lower(), price, currency)
    before = _platform(currency)
    other = _platform("USD" if currency == "SYP" else "SYP")

    payment = _pay(_subscription(buyer, plan), django_capture_on_commit_callbacks,
                   tag=currency)

    assert (payment.amount, payment.currency) == (D(price), currency)
    assert _platform(currency) - before == D(price), "the platform did not get the money"
    assert _platform("USD" if currency == "SYP" else "SYP") == other, \
        "money landed in the other currency's wallet"
    assert _balance(buyer) == D("0.00"), "the plan's price was left in the client's wallet"

    legs = Transaction.objects.filter(metadata__original_payment_id=str(payment.id))
    assert {leg.currency for leg in legs} == {currency}
    assert {leg.tx_type for leg in legs} == {"topup", "transfer"}


@pytest.mark.parametrize("currency,price", [("SYP", "5000.00"), ("USD", "10.00")])
def test_a_refund_takes_the_money_out_of_the_right_platform_wallet(
        make_user, django_capture_on_commit_callbacks, currency, price):
    """The other half of the journey. A refund used to withdraw the ACCESS and write
    nothing to the ledger; with two platform wallets it also has to come out of the
    right one, which it does because every leg is reversed against the wallets it
    actually named."""
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction

    buyer = _person(make_user, f"refund-{currency.lower()}", currency)
    plan = _plan(f"refund-{currency.lower()}", price, currency)
    before = _platform(currency)
    payment = _pay(_subscription(buyer, plan), django_capture_on_commit_callbacks,
                   tag=f"refund-{currency}")
    assert _platform(currency) - before == D(price)

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(payment.id, reason="chargeback")

    assert _platform(currency) == before, "the platform kept a refunded payment"
    assert _balance(buyer) == D("0.00"), "the minted credit was never taken back"
    assert Transaction.objects.filter(
        tx_type="reversal", metadata__original_payment_id=str(payment.id)).count() == 2


def test_a_payment_can_never_move_money_into_a_wallet_holding_the_other_currency(
        make_user, django_capture_on_commit_callbacks):
    """A PERSON's wallet is one currency for ever, and there is no converting on the
    way in: a payment that does not match it writes NO leg at all, rather than a
    plausible one at 1:1.

    The platform's wallets cannot hit this any more — they are addressed by currency —
    so the case that remains is the one that matters: the customer's own.
    """
    from subscription.models import Payment
    from subscription.services.payment_service import PaymentService
    from wallet.models import Transaction

    buyer = _person(make_user, "mismatch", "SYP")
    _fund(buyer, "1.00", "SYP")          # touched, so the currency is now a fact
    plan = _plan("mismatch", "10.00", "USD")
    sub = _subscription(buyer, plan)
    # Written directly, in a currency the checkout would never have produced: the
    # checkout converts into the buyer's own. This is the leg-level guard behind it.
    foreign = Payment.objects.create(subscription=sub, amount=D("10.00"),
                                     currency="USD", status="pending",
                                     payment_method="manual")
    usd_before, syp_before = _platform("USD"), _platform("SYP")

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.complete_payment(foreign.id, {
            "amount": "10.00", "currency": "USD", "status": "completed",
            "transaction_id": "tx-mismatch"})

    assert not Transaction.objects.filter(
        metadata__original_payment_id=str(foreign.id)).exists(), \
        "a leg was written for a payment in a currency the wallet does not hold"
    assert (_platform("USD"), _platform("SYP")) == (usd_before, syp_before)
    assert _balance(buyer) == D("1.00")
    foreign.refresh_from_db()
    assert foreign.payout_settled_at is not None, "the sweep will ask about this for ever"


# ============================================ buying across currencies, at the rate
def test_a_usd_plan_bought_by_a_syp_person_charges_the_converted_amount(
        make_user, rate, django_capture_on_commit_callbacks):
    """The owner: every service can be bought in either currency, and the buyer pays in
    theirs. So the plan keeps the price of record and the CHARGE is converted, once, at
    the operator's live rate — and the rate is written onto the payment, because a
    refund reverses at what was paid and a receipt has to be able to say what happened.
    """
    buyer = _person(make_user, "cross-syp", "SYP")
    plan = _plan("cross", "10.00", "USD")
    before = _platform("SYP")

    payment = _pay(_subscription(buyer, plan), django_capture_on_commit_callbacks,
                   tag="cross")

    assert (payment.amount, payment.currency) == (D("130000.00"), "SYP")
    assert (payment.price_amount, payment.price_currency) == (D("10.00"), "USD")
    assert payment.exchange_rate_id == rate.pk
    assert payment.exchange_rate_value == RATE
    # In the BUYER's currency, in the platform's wallet for the buyer's currency.
    assert _platform("SYP") - before == D("130000.00")
    assert _platform("USD") == D("0.00")
    assert _balance(buyer) == D("0.00")


def test_a_syp_plan_bought_by_a_usd_person_charges_the_converted_amount(
        make_user, rate, django_capture_on_commit_callbacks):
    """The same journey the other way round, so the division is exercised as well as
    the multiplication and a reversed pair would be caught."""
    buyer = _person(make_user, "cross-usd", "USD")
    plan = _plan("cross-back", "130000.00", "SYP")
    before = _platform("USD")

    payment = _pay(_subscription(buyer, plan), django_capture_on_commit_callbacks,
                   tag="cross-back")

    assert (payment.amount, payment.currency) == (D("10.00"), "USD")
    assert (payment.price_amount, payment.price_currency) == (D("130000.00"), "SYP")
    assert _platform("USD") - before == D("10.00")
    assert _platform("SYP") == D("0.00")


def test_a_converted_purchase_is_refunded_at_the_rate_it_was_bought_at(
        make_user, rate, django_capture_on_commit_callbacks):
    """THE POINT OF THE SNAPSHOT. The pound moves — that is why the rate is
    operator-set — so a refund priced at today's rate would hand the customer a
    different number than they paid and move the difference onto or off the platform,
    purely from the clock. The ledger legs carry their own amounts, so the reversal is
    arithmetically the same money whatever the rate says now.
    """
    from subscription.services.payment_service import PaymentService
    from wallet.models import ExchangeRate

    buyer = _person(make_user, "refund-cross", "SYP")
    plan = _plan("refund-cross", "10.00", "USD")
    before = _platform("SYP")
    payment = _pay(_subscription(buyer, plan), django_capture_on_commit_callbacks,
                   tag="refund-cross")
    assert _platform("SYP") - before == D("130000.00")

    # The pound halves overnight. A new row, because a rate is history, not a setting.
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("26000.000000"),
                                effective_from=timezone.now(), note="devaluation")

    with django_capture_on_commit_callbacks(execute=True):
        PaymentService.refund_payment(payment.id, reason="chargeback")

    assert _platform("SYP") == before, "the refund was re-valued at the new rate"
    assert _balance(buyer) == D("0.00")
    payment.refresh_from_db()
    assert payment.exchange_rate_value == RATE, "the snapshot moved with the new rate"


def test_a_stale_rate_stops_a_conversion_and_nothing_else(
        make_user, settings, django_capture_on_commit_callbacks):
    """A rate nobody has refreshed is a money bug that looks like nothing at all, so a
    CONVERSION refuses with its own code. A SAME-currency purchase never consults the
    rate, and must keep selling: most of the market is pounds buying pounds, and
    stopping that because an operator went on holiday would be a far worse bug than
    the one being prevented.
    """
    from wallet.models import ExchangeRate

    settings.EXCHANGE_RATE_MAX_AGE_DAYS = 7
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(days=30),
                                note="ancient")

    syp_person = _person(make_user, "stale-same", "SYP")
    syp_plan = _plan("stale-same", "5000.00", "SYP")
    before = _platform("SYP")
    _pay(_subscription(syp_person, syp_plan), django_capture_on_commit_callbacks,
         tag="stale-same")
    assert _platform("SYP") - before == D("5000.00"), \
        "a same-currency sale was blocked by a rate it never reads"

    from wallet.exchange import StaleExchangeRate

    cross_person = _person(make_user, "stale-cross", "SYP")
    usd_plan = _plan("stale-cross", "10.00", "USD")
    with pytest.raises(StaleExchangeRate) as caught:
        _pay(_subscription(cross_person, usd_plan),
             django_capture_on_commit_callbacks, tag="stale-cross")
    assert caught.value.code == "exchange_rate_stale"


def test_the_checkout_endpoint_refuses_a_stale_conversion_with_its_code(
        make_user, api, settings):
    """What the app actually sees. It branches on `code`, never on the message."""
    from wallet.models import ExchangeRate

    settings.EXCHANGE_RATE_MAX_AGE_DAYS = 7
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(days=30))
    buyer = _person(make_user, "stale-api", "SYP")
    sub = _subscription(buyer, _plan("stale-api", "10.00", "USD"))

    answer = api(buyer).post("/api/subscription/v1/payments/initiate/",
                             {"gateway": "shamcash", "subscription_id": str(sub.id)},
                             content_type=JSON)
    assert answer.status_code == 409, answer.content
    assert answer.json()["code"] == "exchange_rate_stale"


def test_with_no_rate_at_all_a_conversion_is_refused_rather_than_guessed(make_user):
    """Nobody has ever set one. There is no constant to fall back to and there must
    not be: a guessed rate is a price the owner did not choose."""
    from wallet.exchange import NoExchangeRate, convert_at_the_operators_rate

    with pytest.raises(NoExchangeRate) as caught:
        convert_at_the_operators_rate(D("10.00"), "USD", "SYP")
    assert caught.value.code == "exchange_rate_missing"
    # ... and a same-currency "conversion" still answers, because it asks no rate.
    assert convert_at_the_operators_rate(D("10.00"), "USD", "USD") == (D("10.00"), None)


def test_a_price_list_answers_one_currency_per_reader_and_never_two(make_user, api, rate):
    """The app must never convert anything itself and must never show two numbers. So
    the plan list is already in the reader's currency, with the price of record beside
    it only as provenance — and a reader in the plan's own currency is told nothing was
    converted."""
    _plan("list", "10.00", "USD")
    syp_reader = _person(make_user, "list-syp", "SYP")
    usd_reader = _person(make_user, "list-usd", "USD")

    def one(user):
        answer = api(user).get("/api/subscription/v1/plans/")
        assert answer.status_code == 200, answer.content
        rows = answer.json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        return [r for r in rows if r["name"] == "two-cur list"][0]

    syp = one(syp_reader)
    assert (syp["price"], syp["currency"]) == ("130000.00", "SYP")
    assert syp["price_converted"] is True
    assert syp["exchange_rate"] == str(RATE)
    assert (syp["price_of_record"], syp["price_of_record_currency"]) == ("10.00", "USD")

    usd = one(usd_reader)
    assert (usd["price"], usd["currency"]) == ("10.00", "USD")
    assert usd["price_converted"] is False and usd["exchange_rate"] is None


def test_a_receipt_for_a_converted_purchase_states_the_rate(
        make_user, rate, django_capture_on_commit_callbacks):
    """A customer who sees a pound figure for a plan they know is priced in dollars has
    to be able to read why, and at what rate, without asking anyone."""
    from subscription.receipts import receipt_data

    buyer = _person(make_user, "receipt-cross", "SYP")
    payment = _pay(_subscription(buyer, _plan("receipt", "10.00", "USD")),
                   django_capture_on_commit_callbacks, tag="receipt")

    data = receipt_data(payment)
    assert (data["amount"], data["currency"]) == ("130000.00", "SYP")
    assert data["exchange"] == {
        "price_of_record": "10.00", "price_of_record_currency": "USD",
        "rate": str(RATE), "rate_text": f"1 USD = {RATE} SYP"}

    same = _person(make_user, "receipt-same", "SYP")
    plain = _pay(_subscription(same, _plan("receipt-same", "5000.00", "SYP")),
                 django_capture_on_commit_callbacks, tag="receipt-same")
    assert receipt_data(plain)["exchange"] is None


# ====================================================================== coaching
def _coached(make_user, api, tag, *, currency, rate_price="100.00", funds="1000.00",
             trainer_currency=None):
    """A client who asked and a trainer who accepted: month 1 is held in escrow."""
    from users.models import TrainerClientRelation

    trainer = _person(make_user, f"tc_t_{tag}", trainer_currency or currency,
                      user_type="trainer", trainer_hourly_rate=D(rate_price),
                      first_name="Rami")
    client = _person(make_user, f"tc_c_{tag}", currency, first_name="Sam")
    _fund(client, funds, currency)
    assert api(client).post("/api/auth/client/request-trainer/",
                            {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                 {"request_id": relation.pk, "action": "approve"},
                                 content_type=JSON)
    assert answered.status_code == 200, answered.content
    relation.refresh_from_db()
    return trainer, client, relation


def _deliver_first_program(api, trainer, client):
    from routine.models import Routine

    routine = Routine.objects.create(name="Month one", created_by=trainer, days=3)
    answer = api(trainer).post(f"/api/routine/routines/{routine.pk}/assign_to_client/",
                               {"client_id": client.pk}, content_type=JSON)
    assert answer.status_code == 200, answer.content


@pytest.mark.parametrize("currency", ["SYP", "USD"])
def test_a_coaching_month_is_held_and_paid_out_in_each_currency(
        make_user, api, settings, currency):
    """Escrow had SIDE_BUGS S-001's shape too, and worse: one escrow wallet meant a
    month in the second currency could not even be HELD. `take_hold` moved client ->
    escrow, `move_funds_atomic` saw two currencies and raised, and the trainer request
    came back a 402. One escrow per currency, and the platform's 20% lands in the
    revenue wallet for the same one.
    """
    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer, client, relation = _coached(make_user, api, f"m1{currency.lower()}",
                                         currency=currency)
    (month,) = relation.periods.all()
    assert (month.number, month.status, month.price, month.currency) == \
        (1, "held", D("100.00"), currency)
    assert _balance(client) == D("900.00")
    platform_before, escrow_before = _platform(currency), _escrow(currency)
    other = "USD" if currency == "SYP" else "SYP"
    other_platform, other_escrow = _platform(other), _escrow(other)

    _deliver_first_program(api, trainer, client)

    month.refresh_from_db()
    assert (month.status, month.commission, month.trainer_share) == \
        ("settled", D("20.00"), D("80.00"))
    assert _balance(trainer) == D("80.00")
    assert _platform(currency) - platform_before == D("20.00")
    assert escrow_before - _escrow(currency) == D("100.00")
    assert (_platform(other), _escrow(other)) == (other_platform, other_escrow), \
        "coaching money touched the other currency's ledger wallets"


def test_a_trainer_paid_in_the_other_currency_is_paid_by_an_explicit_exchange(
        make_user, api, settings, rate):
    """The one place coaching money crosses a currency. The client paid in theirs and
    the trainer is owed in theirs; escrow cannot hold both and a person's wallet cannot
    change unit, so this is an exchange with TWO legs — a debit out of escrow in the
    month's currency, a credit into the trainer's in theirs — both naming the rate row.

    A single leg would be a transfer at 1:1, which is the bug the whole currency guard
    exists for.
    """
    from wallet.models import Transaction

    settings.COACHING_COMMISSION_RATE = D("0.20")
    # The trainer prices in dollars; the client trades in pounds and is charged in
    # pounds: 100 USD -> 1,300,000 SYP.
    trainer, client, relation = _coached(
        make_user, api, "cross", currency="SYP", trainer_currency="USD",
        rate_price="100.00", funds="2000000.00")
    (month,) = relation.periods.all()
    assert (month.price, month.currency) == (D("1300000.00"), "SYP")

    platform_before = _platform("SYP")
    _deliver_first_program(api, trainer, client)
    month.refresh_from_db()

    # The split happens in the month's own currency, and only the trainer's share
    # crosses. The platform's cut stays in the books the month was sold in.
    assert (month.commission, month.trainer_share) == (D("260000.00"), D("1040000.00"))
    assert _platform("SYP") - platform_before == D("260000.00")
    assert _balance(trainer) == D("80.00")       # 1,040,000 SYP at 13,000

    legs = list(Transaction.objects.filter(metadata__period_id=month.pk,
                                           metadata__purpose="trainer_assignment_settlement")
                .union(Transaction.objects.filter(
                    exchange_reference__in=Transaction.objects.filter(
                        metadata__period_id=month.pk).exclude(exchange_reference="")
                    .values_list("exchange_reference", flat=True))))
    pair = [leg for leg in legs if leg.exchange_reference]
    assert len(pair) == 2, [(leg.tx_type, leg.currency, leg.amount) for leg in legs]
    debit = [leg for leg in pair if leg.source_wallet_id and not leg.destination_wallet_id]
    credit = [leg for leg in pair if leg.destination_wallet_id and not leg.source_wallet_id]
    assert len(debit) == len(credit) == 1, "an exchange that is not two one-sided legs"
    debit, credit = debit[0], credit[0]
    assert {leg.exchange_reference for leg in pair} == {debit.reference_id}
    assert debit.exchange_rate_id == credit.exchange_rate_id == rate.pk

    # THE TWO LEGS RECONCILE TO THE UNIT, at the rate they both name — nothing was
    # invented and nothing was destroyed on the way across.
    from wallet.exchange import convert_at_the_operators_rate

    assert (debit.currency, credit.currency) == ("SYP", "USD")
    assert convert_at_the_operators_rate(debit.amount, "SYP", "USD")[0] == credit.amount
    assert debit.amount == month.trainer_share


# ==================================================================== the rate itself
def test_a_rate_is_append_only_and_an_old_transaction_keeps_its_value(
        make_user, rate, django_capture_on_commit_callbacks):
    """Every past purchase's meaning depends on the rate that was live when it
    happened. Editing or deleting a row would silently re-value them, so neither is
    possible — the correction for a bad rate is a better rate, appended."""
    from wallet.models import ExchangeRate, Transaction

    buyer = _person(make_user, "fx-history", "SYP")
    payment = _pay(_subscription(buyer, _plan("fx-history", "10.00", "USD")),
                   django_capture_on_commit_callbacks, tag="fx-history")
    legs = list(Transaction.objects.filter(
        metadata__original_payment_id=str(payment.id)).order_by("id"))
    before = [(leg.amount, leg.currency) for leg in legs]

    with pytest.raises(PermissionError):
        rate.rate = D("1.000000")
        rate.save()
    with pytest.raises(PermissionError):
        ExchangeRate.objects.get(pk=rate.pk).delete()

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("26000.000000"),
                                effective_from=timezone.now())
    for leg in legs:
        leg.refresh_from_db()
    assert [(leg.amount, leg.currency) for leg in legs] == before
    payment.refresh_from_db()
    assert payment.amount == D("130000.00") and payment.exchange_rate_value == RATE


def test_the_rate_now_is_the_newest_one_that_has_started(db):
    """An operator may date a rate ahead. Until it starts, it is queued, not live."""
    from wallet.exchange import current_rate
    from wallet.models import ExchangeRate

    now = timezone.now()
    live = ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("13000.000000"),
                                       effective_from=now - timedelta(days=1))
    ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("14000.000000"),
                                effective_from=now + timedelta(days=1))
    assert current_rate().pk == live.pk


def test_rounding_is_one_rule_and_never_invents_or_destroys_a_unit(rate):
    """Two decimal places, ROUND_HALF_UP, the same as `users.coaching.split` — stated
    in `wallet/exchange.py` and asserted here so it cannot drift."""
    from wallet.exchange import convert_at_the_operators_rate

    # A half-unit rounds up, not to even.
    assert convert_at_the_operators_rate(D("0.000005"), "USD", "SYP")[0] == D("0.07")
    assert convert_at_the_operators_rate(D("1.00"), "USD", "SYP")[0] == D("13000.00")
    assert convert_at_the_operators_rate(D("13000.00"), "SYP", "USD")[0] == D("1.00")


def test_only_the_two_named_callers_convert_a_currency():
    """One conversion boundary, not a helper anyone can call. A conversion scattered
    across views is how a rate gets applied twice, or applied and not snapshotted —
    and neither shows up as an error, only as money. Same shape as the source scan in
    `tests/test_policy_money.py`."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    skipped = {".venv", "_excluded", "tests", "migrations", "mobile", "flutter",
               "node_modules", "staticfiles", "static", "media", ".claude", ".git"}
    #: The two the owner's rule allows: the purchase (which is also the price list,
    #: through `price_for`) and the one movement of money that crosses currencies.
    allowed = {"subscription/pricing.py", "wallet/exchange.py", "wallet/models.py"}

    callers = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if skipped & set(rel.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            name = (getattr(getattr(node, "func", None), "id", None)
                    or getattr(getattr(node, "func", None), "attr", None))
            if isinstance(node, ast.Call) and name == "convert_at_the_operators_rate":
                callers.add(str(rel))
    assert callers <= allowed, sorted(callers - allowed)


# ==================================================================== the migration
def test_the_migration_leaves_the_platforms_existing_balance_where_it_is(db):
    """`wallet/0016` renames the ledger OWNER, never the wallet.

    The platform's revenue and escrow wallets may already hold a balance. The wallet
    row keeps its primary key, its balance and every `Transaction` foreign key pointing
    at it; only the owner's `email`/`username` gain the currency they already held,
    read off the wallet's own column rather than guessed from a setting — a deployment
    that ran with `PLATFORM_CURRENCY=USD` holds dollars in it.
    """
    import importlib

    from django.apps import apps
    from django.contrib.auth import get_user_model

    from wallet.models import Wallet

    migration = importlib.import_module(
        "wallet.migrations.0016_two_currencies_a_ledger_wallet_for_each")

    User = get_user_model()
    # A ledger owner in the OLD shape, holding money, exactly as the migration finds it.
    owner = User.objects.create(email="platform_revenue@local",
                                username="platform_revenue", user_type="agent",
                                is_active=False, phone_number="0000000000")
    owner.set_unusable_password()
    owner.save(update_fields=["password"])
    wallet = Wallet.objects.create(owner=owner, owner_type="agent", currency="USD",
                                   balance=D("4321.00"))

    migration.name_the_ledger_owners_by_currency(apps, None)

    owner.refresh_from_db()
    wallet.refresh_from_db()
    assert owner.email == "platform_revenue_usd@local"
    assert owner.username == "platform_revenue_usd"
    assert (wallet.pk, wallet.balance, wallet.currency) == (wallet.pk, D("4321.00"), "USD")
    # And the helper now finds that very wallet for USD, with its money in it.
    from wallet.utils import get_platform_revenue_wallet

    assert get_platform_revenue_wallet("USD").pk == wallet.pk
    assert get_platform_revenue_wallet("USD").balance == D("4321.00")

    # Reversible, and it puts back the address the old code knows.
    migration.name_the_ledger_owners_without_a_currency(apps, None)
    owner.refresh_from_db()
    assert owner.email == "platform_revenue@local"
