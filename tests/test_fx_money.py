"""Money in two currencies: the defects the money fixer closed (SIDE_BUGS, 2026-09-23).

One test per row, each written against the journey that went wrong rather than the
helper that was changed: a trainer re-priced by a client's month, cash minted in the
wrong currency, a stale rate that froze a paid coaching, a price shown in somebody
else's unit.

| Test | Row |
|---|---|
| `test_s058_...` | an untouched trainer wallet took the client's currency on payout |
| `test_s059_...` | an agent top-up carried no currency; caps added the two together |
| `test_s086_...` | a person could take a ledger owner's username first |
| `test_s156_...` | the rate form accepted the reverse pair and called it live |
| `test_s157_...` | the rate history ran one query per row |
| `test_s158_...` | a currency decided on an unlocked read |
| `test_s159_...` | a currency switch while a checkout was open |
| `test_s160_...` | a cross-currency transfer was a bare 400 |
| `test_s161_...` | one leg of an exchange could be reversed alone |
| `test_s162_...` | one alert threshold for both currencies, plan payments flagged |
| `test_s082_...` | a stale rate blocked the client's same-currency charge |
| `test_s083_...` | `agreed_rate` had no currency |
| `test_s084_...` | the request pre-check compared two currencies' numbers |
| `test_s085_...` | the approval dropped the refusal's code |
| `test_s151_...` | a converted coaching charge kept no rate |
| `test_s041_...` | trainer prices listed in the trainer's currency, no unit |
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
D = Decimal
#: 1 USD = 13,000 SYP, round so every figure below can be read.
RATE = D("13000.000000")


# --------------------------------------------------------------------- helpers
@pytest.fixture
def rate(db):
    from wallet.models import ExchangeRate

    return ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                       effective_from=timezone.now() - timedelta(hours=1),
                                       note="fx-money")


def _make_stale(rate_row):
    """Age the live rate past the limit. `update()`: the model refuses an edit."""
    from wallet.models import ExchangeRate

    ExchangeRate.objects.filter(pk=rate_row.pk).update(
        effective_from=timezone.now() - timedelta(days=30))


def _person(make_user, name, currency, *, user_type="client", **kw):
    from wallet.models import set_currency_for

    user = make_user(name, user_type=user_type, **kw)
    set_currency_for(user, currency, user_type)
    return user


def _fund(user, amount):
    from wallet.models import Wallet

    Wallet.objects.filter(owner=user).update(balance=D(amount))


def _wallet(user):
    from wallet.models import Wallet

    return Wallet.objects.get(owner=user)


def _relation(trainer, client, rate_price):
    """A pending request carrying the price, the way the request endpoint writes it."""
    from users.models import TrainerClientRelation

    return TrainerClientRelation.objects.create(
        trainer=trainer, client=client, status="pending", initiated_by="client",
        agreed_rate=D(rate_price), requested_at=timezone.now())


def _approve(api, trainer, relation):
    return api(trainer).post("/api/auth/trainer/respond-to-request/",
                             {"request_id": relation.pk, "action": "approve"},
                             content_type=JSON)


# ==================================================================== S-058
def test_s058_a_payout_in_the_clients_currency_never_flips_the_trainers(make_user, rate):
    """A trainer who never chose a currency prices at 500000 — pounds, their wallet's.
    A dollar client's month 1 is held at about $38. Paying it out used to hand the
    trainer's untouched wallet to dollars, and from then on 500000 was read as
    $500,000. The trainer is paid in pounds, by an exchange, and stays in pounds."""
    from users.trainer_relations import settle_hold, take_hold
    from wallet import currency_for

    trainer = make_user("fx58_t", user_type="trainer", trainer_hourly_rate=D("500000.00"))
    assert (_wallet(trainer).currency, _wallet(trainer).currency_chosen_at) == ("SYP", None)
    client = _person(make_user, "fx58_c", "USD")
    _fund(client, "100.00")
    relation = _relation(trainer, client, "500000.00")

    take_hold(relation, trainer)
    (month,) = relation.periods.all()
    assert (month.price, month.currency) == (D("38.46"), "USD")

    settle_hold(relation, trainer)

    wallet = _wallet(trainer)
    assert wallet.currency == "SYP", "the client's month chose the trainer's currency"
    assert currency_for(trainer) == "SYP"
    # The whole month crosses (commission is 0 in these tests): $38.46 at 13,000.
    assert wallet.balance == D("499980.00")


# ==================================================================== S-059
def _agent(make_user, name, **limits):
    from wallet.models import ensure_agent_profile

    agent = make_user(name, user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    for field, value in limits.items():
        setattr(profile, field, D(value))
    profile.save()
    return agent, profile


def test_s059_an_agent_top_up_names_its_currency_and_is_capped_in_it(make_user, api):
    """Cash does not come labelled. A dollar client handed 500,000 pounds of cash was
    credited 500,000 DOLLARS, and a cap set in pounds let the same number of dollars
    through."""
    from wallet.models import Transaction

    client = _person(make_user, "fx59_c", "USD")
    agent, profile = _agent(make_user, "fx59_a", daily_limit="1000000",
                            monthly_limit="1000000")
    url = "/api/wallet/agent/topup/proxy/"

    def topup(amount, currency, key):
        body = {"client_identifier": str(client.id), "amount": amount,
                "idempotency_key": key}
        if currency:
            body["currency"] = currency
        return api(agent).post(url, body, content_type=JSON)

    # Pounds of cash into a dollar wallet: refused, not relabelled.
    wrong = topup("500000.00", "SYP", "fx59-1")
    assert wrong.status_code == 409, wrong.content
    assert (wrong.json()["code"], wrong.json()["client_currency"],
            wrong.json()["topup_currency"]) == ("currency_mismatch", "USD", "SYP")
    assert _wallet(client).balance == D("0")

    # No currency at all: the caller must say.
    missing = topup("50.00", None, "fx59-2")
    assert missing.status_code == 400
    assert "currency" in missing.json()["field_errors"]

    # Dollars, but nobody has set a dollar cap: the caps fail closed.
    capped = topup("50.00", "USD", "fx59-3")
    assert capped.status_code == 429, capped.content
    assert _wallet(client).balance == D("0")

    # With a dollar cap, dollars go in, counted against it and not against pounds.
    profile.daily_limit_usd = profile.monthly_limit_usd = D("100.00")
    profile.save()
    ok = topup("60.00", "USD", "fx59-4")
    assert ok.status_code == 200, ok.content
    assert (ok.json()["balance"], ok.json()["currency"]) == ("60.00", "USD")
    assert topup("60.00", "USD", "fx59-5").status_code == 429, "60 + 60 > the $100 cap"
    row = Transaction.objects.get(reference_id=ok.json()["reference_id"])
    assert (row.currency, row.amount) == ("USD", D("60.00"))


def test_s059_the_signed_top_up_signs_the_currency(make_user, api):
    import time

    from wallet.models import AgentAPIKey
    from wallet.security import compute_hmac_signature, encrypt_secret

    client = make_user("fx59s_c")                       # a pound client
    agent, profile = _agent(make_user, "fx59s_a", daily_limit="100000",
                            monthly_limit="100000")
    secret = "s" * 64
    AgentAPIKey.objects.create(agent=profile, key_id="fx59signed", hashed_key="x",
                               secret_ciphertext=encrypt_secret(secret), is_active=True)
    ts = int(time.time())

    def post(currency, signed_currency, key):
        message = f"{client.id}|500.00|{ts}|{key}"
        if signed_currency:
            message += f"|{signed_currency}"
        sig = compute_hmac_signature(secret, message)
        return api(agent).post(
            "/api/wallet/agent/topup/",
            {"client_identifier": str(client.id), "amount": "500.00", "currency": currency,
             "idempotency_key": key, "timestamp": ts, "signature": sig},
            content_type=JSON,
            HTTP_X_AGENT_AUTH=f"AgentAuth key_id=fx59signed,signature={sig},timestamp={ts}")

    # A signature over the old four fields does not cover the currency: refused.
    assert post("SYP", None, "fx59s-1").status_code == 401
    good = post("SYP", "SYP", "fx59s-2")
    assert good.status_code == 200, good.content
    assert good.json()["currency"] == "SYP"


# ==================================================================== S-086
def test_s086_nobody_can_take_a_ledger_owners_name(make_user, rf):
    """A signup as `platform_revenue_usd` made the first dollar revenue wallet raise
    IntegrityError, so every dollar plan payout stayed owed for ever."""
    from django.contrib.auth import get_user_model

    from users.serializers import CustomRegisterSerializer
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    form = CustomRegisterSerializer(
        data={"username": "Platform_Escrow_USD", "email": "fx86@gate.test",
              "phone_number": "+963911111111", "password1": "Xx!23456long",
              "password2": "Xx!23456long", "date_of_birth": "1990-01-01"},
        context={"request": rf.post("/api/auth/register/")})
    assert not form.is_valid(), "a ledger owner's username was accepted"
    assert [e.code for e in form.errors.get("username", [])] == ["username_taken"]

    # A name taken before it was reserved (or by hand) no longer breaks the books.
    get_user_model().objects.create_user(email="squatter@gate.test",
                                         username="platform_revenue_usd",
                                         password="Xx!23456")
    revenue = get_platform_revenue_wallet("USD")
    assert (revenue.currency, revenue.owner.email) == ("USD", "platform_revenue_usd@local")
    assert revenue.owner.username != "platform_revenue_usd"
    assert get_platform_revenue_wallet("USD").pk == revenue.pk, "found again by address"
    assert get_escrow_wallet("USD").currency == "USD"


# ==================================================================== S-156
def _console(make_user, name):
    from django.test import Client

    operator = make_user(name, user_type="admin")
    console = Client()
    console.force_login(operator)
    return console


def test_s156_the_rate_form_sets_the_one_pair_conversions_read(make_user):
    """The form offered base and quote. "1 SYP = x USD" was saved, shown as live, and
    never read by a conversion, which kept using the stale USD/SYP row."""
    from wallet.admin import ExchangeRateAdmin
    from wallet.exchange import current_rate
    from wallet.models import ExchangeRate

    console = _console(make_user, "fx156_ops")
    now = timezone.localtime()
    saved = console.post("/dj-admin/wallet/exchangerate/add/", {
        "base": "SYP", "quote": "USD", "rate": "13500",
        "effective_from_0": now.strftime("%Y-%m-%d"),
        "effective_from_1": (now - timedelta(minutes=1)).strftime("%H:%M:%S"),
        "note": "fx156"})
    assert saved.status_code == 302, saved.content[:2000]
    row = ExchangeRate.objects.get(note="fx156")
    assert (row.base, row.quote) == ("USD", "SYP")
    assert current_rate().pk == row.pk

    # A reverse-pair row (from before) is never labelled live.
    reverse = ExchangeRate.objects.create(base="SYP", quote="USD", rate=D("0.000077"),
                                          effective_from=timezone.now(), note="rev")
    from django.contrib import admin as dj_admin

    label = str(ExchangeRateAdmin(ExchangeRate, dj_admin.site).status(reverse))
    assert "live" not in label.lower(), label


# ==================================================================== S-157
def test_s157_the_rate_history_reads_the_live_rate_once(make_user):
    """The status column asked for the current rate once per row."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from wallet.models import ExchangeRate

    console = _console(make_user, "fx157_ops")

    def page_cost():
        with CaptureQueriesContext(connection) as queries:
            assert console.get("/dj-admin/wallet/exchangerate/").status_code == 200
        return len(queries)

    for day in range(2):
        ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("13000"),
                                    effective_from=timezone.now() - timedelta(days=day + 1))
    few = page_cost()
    for day in range(5):
        ExchangeRate.objects.create(base="USD", quote="SYP", rate=D("13000"),
                                    effective_from=timezone.now() - timedelta(days=day + 10))
    assert page_cost() == few, "the rate history costs a query per row again"


# ==================================================================== S-158
def test_s158_a_currency_is_decided_on_the_locked_row(make_user):
    """Two halves of one race. The ledger checked the CALLER's copy of a wallet, so a
    copy read before the wallet changed unit wrote the old unit into it at 1:1; and
    `wallet_for` decided "untouched" and rewrote the currency without a lock."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from wallet.models import Wallet, move_funds_atomic, wallet_for

    person = make_user("fx158_p")
    stale = Wallet.objects.get(owner=person)                 # read while it is SYP
    Wallet.objects.filter(pk=stale.pk).update(currency="USD")  # changed under us
    with pytest.raises(ValueError):
        move_funds_atomic(None, stale, D("10.00"), currency="SYP", tx_type="topup")
    fresh = Wallet.objects.get(pk=stale.pk)
    assert (fresh.balance, fresh.currency) == (D("0"), "USD")

    other = make_user("fx158_q")
    with CaptureQueriesContext(connection) as queries:
        assert wallet_for(other, "client", "USD").currency == "USD"
    locked = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]
              and "wallet_wallet" in q["sql"]]
    assert locked, "the currency was rewritten without locking the wallet"


# ==================================================================== S-159
def test_s159_no_currency_switch_while_a_checkout_is_open(make_user, api):
    """A plan checkout opened in pounds, then a switch to dollars: the completed
    payment found a dollar wallet and was recorded as settled with no ledger leg."""
    from subscription.models import Subscription, SubscriptionPlan
    from subscription.services.payment_service import PaymentService

    buyer = make_user("fx159_c")
    plan = SubscriptionPlan.objects.create(name="fx159", plan_type="premium",
                                           description="x", price=D("5000.00"),
                                           currency="SYP", duration_days=30,
                                           is_active=True)
    sub = Subscription.objects.create(user=buyer, plan=plan, status="pending",
                                      start_date=timezone.now(),
                                      end_date=timezone.now() + timedelta(days=30))
    PaymentService.start_payment(sub, lambda p: ("manual", "ref-fx159", {}))

    answer = api(buyer).put("/api/wallet/currency/", {"currency": "USD"},
                            content_type=JSON)
    assert answer.status_code == 409, answer.content
    assert (answer.json()["code"], answer.json()["currency"]) == \
        ("currency_locked_open_checkout", "SYP")
    assert _wallet(buyer).currency == "SYP"
    # Asking for the currency it already has is not a switch.
    assert api(buyer).put("/api/wallet/currency/", {"currency": "SYP"},
                          content_type=JSON).status_code == 200


# ==================================================================== S-160
def test_s160_a_cross_currency_transfer_is_refused_with_a_code(make_user, api):
    client = _person(make_user, "fx160_c", "SYP")
    _fund(client, "1000.00")
    dollar_trainer = _person(make_user, "fx160_t", "USD", user_type="trainer")
    pound_trainer = _person(make_user, "fx160_p", "SYP", user_type="trainer")
    url = "/api/wallet/client/transfer/"

    crossed = api(client).post(url, {"trainer_id": dollar_trainer.pk, "amount": "100.00",
                                     "idempotency_key": "fx160-1"}, content_type=JSON)
    assert crossed.status_code == 409, crossed.content
    assert (crossed.json()["code"], crossed.json()["client_currency"],
            crossed.json()["trainer_currency"]) == ("currency_mismatch", "SYP", "USD")

    short = api(client).post(url, {"trainer_id": pound_trainer.pk, "amount": "5000.00",
                                   "idempotency_key": "fx160-2"}, content_type=JSON)
    assert short.status_code == 402, short.content
    assert (short.json()["code"], short.json()["currency"]) == ("insufficient_balance", "SYP")
    assert _wallet(client).balance == D("1000.00")


# ==================================================================== S-161
def test_s161_one_half_of_an_exchange_is_not_reversed_alone(make_user, api, rate):
    """Reversing only the trainer's credit burned their money while the escrow debit
    stayed burned too: the month's money vanished from both books."""
    from wallet.models import Transaction, exchange_funds_atomic
    from wallet.utils import get_escrow_wallet

    trainer = _person(make_user, "fx161_t", "USD", user_type="trainer")
    escrow = get_escrow_wallet("SYP")
    escrow.balance = D("130000.00")
    escrow.save(update_fields=["balance"])
    debit, credit = exchange_funds_atomic(escrow, _wallet(trainer), D("130000.00"),
                                          amount_in="SYP")
    admin = make_user("fx161_admin", user_type="admin")

    for leg in (credit, debit):
        answer = api(admin).post("/api/wallet/admin/reversal/",
                                 {"reference_id": leg.reference_id,
                                  "idempotency_key": f"fx161-{leg.pk}"},
                                 content_type=JSON)
        assert answer.status_code == 409, answer.content
        assert answer.json()["code"] == "exchange_leg"
    assert not Transaction.objects.filter(tx_type="reversal").exists()
    assert _wallet(trainer).balance == D("10.00")


# ==================================================================== S-162
def test_s162_large_top_ups_are_judged_per_currency_and_plans_are_not_top_ups(
        make_user, api, settings):
    from wallet.models import move_funds_atomic, wallet_for

    settings.WALLET_LARGE_TOPUP_THRESHOLDS = {"SYP": D("1000000"), "USD": D("100")}
    pound_agent = make_user("fx162_ap", user_type="agent")
    dollar_agent = make_user("fx162_ad", user_type="agent")
    pound_client = _person(make_user, "fx162_cp", "SYP")
    dollar_client = _person(make_user, "fx162_cd", "USD")
    plan_buyer = make_user("fx162_plan")

    # 5,000 pounds of cash: about 38 cents. Not large.
    move_funds_atomic(None, wallet_for(pound_client, "client"), D("5000.00"),
                      actor_id=pound_agent.pk, tx_type="topup", currency="SYP",
                      metadata={"agent_id": pound_agent.pk})
    # $500 of cash: 6.5 million pounds. Large.
    move_funds_atomic(None, wallet_for(dollar_client, "client"), D("500.00"),
                      actor_id=dollar_agent.pk, tx_type="topup", currency="USD",
                      metadata={"agent_id": dollar_agent.pk})
    # The mint leg of a plan payment is not a cash top-up at all.
    move_funds_atomic(None, wallet_for(plan_buyer, "client"), D("2000000.00"),
                      actor_id=plan_buyer.pk, tx_type="topup", currency="SYP",
                      metadata={"original_payment_id": "123"})

    admin = make_user("fx162_admin", user_type="admin")
    alerts = api(admin).get("/api/wallet/admin/alerts/suspicious/").json()["alerts"]
    large = [(a["actor_id"], a.get("currency")) for a in alerts if a["type"] == "large_topups"]
    assert large == [(dollar_agent.pk, "USD")], large


# ==================================================================== S-082
def test_s082_a_stale_rate_owes_the_trainer_and_never_blocks_the_month(
        make_user, api, rate, settings):
    """The client pays in pounds and the platform keeps its cut in pounds; only the
    trainer's share crosses to dollars. With a stale rate that crossing used to raise
    AFTER the client's charge, so month 1 stayed 'held' and every renewal rolled back
    while the coaching carried on unpaid. Now the month runs, the share waits in
    escrow, and the hourly run pays it once the rate is fresh."""
    from users.coaching import renew_due
    from users.trainer_relations import settle_hold
    from wallet.models import ExchangeRate
    from wallet.utils import get_escrow_wallet, get_platform_revenue_wallet

    settings.COACHING_COMMISSION_RATE = D("0.20")
    trainer = _person(make_user, "fx82_t", "USD", user_type="trainer",
                      trainer_hourly_rate=D("100.00"))
    client = _person(make_user, "fx82_c", "SYP")
    _fund(client, "2000000.00")
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    from users.models import TrainerClientRelation

    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    assert _approve(api, trainer, relation).status_code == 200
    relation.refresh_from_db()
    platform_before = get_platform_revenue_wallet("SYP").balance
    escrow_before = get_escrow_wallet("SYP").balance

    _make_stale(rate)
    settle_hold(relation, trainer)            # used to raise HoldFailed

    (month,) = relation.periods.all()
    assert month.status == "settled" and month.starts_at is not None
    assert month.payout_owed_since is not None and month.payout_reference == ""
    assert get_platform_revenue_wallet("SYP").balance - platform_before == D("260000.00")
    assert escrow_before - get_escrow_wallet("SYP").balance == D("260000.00"), \
        "only the commission left escrow; the trainer's share is still owed there"
    assert _wallet(trainer).balance == D("0")

    ExchangeRate.objects.create(base="USD", quote="SYP", rate=RATE,
                                effective_from=timezone.now() - timedelta(minutes=5))
    counts = renew_due()["counts"]
    assert counts.get("trainer_payouts_settled") == 1, counts
    month.refresh_from_db()
    assert month.payout_owed_since is None and month.payout_reference
    assert _wallet(trainer).balance == D("80.00")


# ==================================================================== S-083
def test_s083_an_agreed_price_keeps_the_currency_it_was_quoted_in(make_user, api, rate):
    """A trainer invites at 500000 in pounds, and their wallet is then in dollars when
    the client accepts. The client agreed to 500000 POUNDS (about $38), not dollars."""
    from users.models import TrainerClientRelation
    from wallet.models import Wallet

    trainer = make_user("fx83_t", user_type="trainer", trainer_hourly_rate=D("500000.00"))
    client = _person(make_user, "fx83_c", "USD")
    _fund(client, "100.00")
    invited = api(trainer).post("/api/auth/trainer/assign-client/", {"client_id": client.pk},
                                content_type=JSON)
    assert invited.status_code == 200, invited.content
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)

    # The trainer's wallet changes unit before the client answers.
    Wallet.objects.filter(owner=trainer).update(currency="USD")

    accepted = api(client).post("/api/auth/client/respond-to-invite/",
                                {"request_id": relation.pk, "action": "approve"},
                                content_type=JSON)
    assert accepted.status_code == 200, accepted.content
    assert _wallet(client).balance == D("61.54"), "charged in the wrong currency"


# ==================================================================== S-084
def test_s084_the_request_check_compares_the_month_in_the_clients_currency(
        make_user, api, rate):
    """A dollar client with $100 asking a 500,000-pound trainer (about $38) was told
    they needed "500000.00"."""
    trainer = _person(make_user, "fx84_t", "SYP", user_type="trainer",
                      trainer_hourly_rate=D("500000.00"))
    client = _person(make_user, "fx84_c", "USD")
    _fund(client, "100.00")
    asked = api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                             content_type=JSON)
    assert asked.status_code == 200, asked.content

    poor = _person(make_user, "fx84_poor", "USD")
    _fund(poor, "10.00")
    refused = api(poor).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                             content_type=JSON)
    assert refused.status_code == 402, refused.content
    assert (refused.json()["code"], refused.json()["required"], refused.json()["balance"],
            refused.json()["currency"]) == ("insufficient_balance", "38.46", "10.00", "USD")


# ==================================================================== S-085
def test_s085_an_approval_refused_for_the_rate_says_so_with_its_code(make_user, api, rate):
    """The code travelled on the exception and the view dropped it: a 402 with the
    operator's English ("... days old ...") in the body."""
    from users.models import TrainerClientRelation

    trainer = _person(make_user, "fx85_t", "USD", user_type="trainer",
                      trainer_hourly_rate=D("100.00"))
    client = _person(make_user, "fx85_c", "SYP")
    _fund(client, "2000000.00")
    assert api(client).post("/api/auth/client/request-trainer/", {"trainer_id": trainer.pk},
                            content_type=JSON).status_code == 200
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)

    _make_stale(rate)
    answer = _approve(api, trainer, relation)
    assert answer.status_code == 409, answer.content
    assert answer.json()["code"] == "exchange_rate_stale"
    assert "days old" not in answer.content.decode(), "the operator's English, shown"
    relation.refresh_from_db()
    assert relation.status == "pending" and not relation.hold_reference


# ==================================================================== S-151
def test_s151_a_converted_month_keeps_its_price_of_record_and_rate(make_user, rate):
    """A dollar-priced month charged in pounds kept neither the rate nor the price it
    was converted from, so it could not be explained or refunded honestly."""
    from users.trainer_relations import take_hold
    from wallet.models import Transaction

    trainer = _person(make_user, "fx151_t", "USD", user_type="trainer",
                      trainer_hourly_rate=D("100.00"))
    client = _person(make_user, "fx151_c", "SYP")
    _fund(client, "2000000.00")
    relation = _relation(trainer, client, "100.00")
    take_hold(relation, trainer)

    hold = Transaction.objects.get(reference_id=relation.hold_reference)
    assert (hold.amount, hold.currency) == (D("1300000.00"), "SYP")
    assert hold.metadata.get("exchange_rate_id") == rate.pk, hold.metadata
    assert D(hold.metadata["exchange_rate_value"]) == RATE
    assert (hold.metadata["price_amount"], hold.metadata["price_currency"]) == \
        ("100.00", "USD")
    (month,) = relation.periods.all()
    assert (month.price_amount, month.price_currency) == (D("100.00"), "USD")
    assert (month.exchange_rate_id, month.exchange_rate_value) == (rate.pk, RATE)


# ==================================================================== S-041
def test_s041_a_trainers_price_is_listed_in_the_readers_currency(make_user, api, rate):
    """A pound client reading a dollar trainer's `100.00` read it as pounds and was then
    charged 1,300,000. Every list now answers the reader's number and names the unit."""
    trainer = _person(make_user, "fx41_t", "USD", user_type="trainer",
                      trainer_hourly_rate=D("100.00"), first_name="Dollarton")
    client = _person(make_user, "fx41_c", "SYP")
    on_apk = {"HTTP_X_APP_CHANNEL": "apk"}

    def number(value):
        # The legacy lists send JSON numbers, as they always have.
        return D(str(value))

    listed = api(client).get("/api/auth/client/available-trainers/?search=Dollarton",
                             **on_apk).json()
    (row,) = listed["results"]["available_trainers"]
    assert number(row["trainer_hourly_rate"]) == D("1300000"), "shown in dollars"
    assert row["coaching_currency"] == "SYP"

    matches = api(client).get("/api/v1/trainers/matches?limit=10", **on_apk).json()
    (card,) = [m["trainer"] for m in matches["results"] if m["trainer"]["id"] == trainer.pk]
    assert card["coaching_price"] == "1300000.00", "shown in dollars"
    assert card["coaching_currency"] == "SYP"

    # Nobody signed in: the price of record, never without its unit.
    public = api().get("/api/auth/trainers/public/?search=Dollarton", **on_apk).json()
    (row,) = public["results"]["available_trainers"]
    assert (number(row["trainer_hourly_rate"]), row.get("coaching_currency")) == \
        (D("100"), "USD")

    _relation(trainer, client, "100.00")
    status_ = api(client).get("/api/auth/client/request-status/", **on_apk).json()
    (req,) = status_["requests"]
    assert number(req["agreed_rate"]) == D("1300000"), "shown in dollars"
    assert req["agreed_currency"] == "SYP"
