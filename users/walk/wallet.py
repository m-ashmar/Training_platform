"""Walk states: wallet and agents (`users/walk/__init__.py` says how to add one).

Owned by the money worker: a wallet topped up by an agent, an agent holding the
platform's cash, a trainer's withdrawals, the client wallets.

**How these states move money.** Through the routes the app calls (`walk.api`: the real
URL and view, as the account, with the build declared), and through the operator's
service functions where an operator acts in the console (`payouts.services`,
`wallet.agents.record_settlement`, `trainer_verification.services`). Nothing here
writes a ledger row, a payment state or a hold: every one is the platform's own write.
The agent's caps and ceiling are the operator's console settings on the agent's
profile, set the way the console sets them.

**One currency per state, never a rate.** Each state trades in its client's own
currency (`money_of`: the wallet's, or for an account no money has reached yet the
platform's base currency, so a new Syrian client's wallet is in pounds). A top-up is in
that currency (`shared.top_up`, split on the agent's per-top-up cap in it) and the
agent's ceiling and settlement are read in it; a trainer is
a fresh account too, so the coaching price (`COACH_RATE`, in the trainer's currency) and
the client's money are the same currency and a transfer never crosses. Nothing here
converts, and nothing creates or sets an exchange rate: that is the operator's value,
and a development database may have none (the app builders' does, on purpose).

The helpers (`call`, `operator`, `agent`, `funded_client`, `coach`, `money_of`) are
shared with `users/walk/coaching.py`, the other money module; the top-up is the walk's
one (`shared.top_up`).
"""
from __future__ import annotations

import uuid
from decimal import ROUND_UP, Decimal
from functools import partial

from . import state
from .framework import agent_cash_gaps, needs, on_retire, withdrawal_gaps
from .shared import top_up

#: A trainer's monthly fee in the walk states, in the trainer's currency (a fresh
#: account's: the platform's base currency, pounds).
COACH_RATE = Decimal("40000.00")

# What the states move, declared once: read by each state AND by its `@needs` (the
# check before anything is written: `framework.agent_cash_gaps`, `withdrawal_gaps`), so
# the check cannot drift from what the state does. In a new wallet's currency.

#: The withdrawal states: what the trainer has earned before asking (sent by one client,
#: whose cash one agent took, with the platform's commission on top: `gross_for`), and
#: the one withdrawal the trainer asks for that day.
EARNED = Decimal("60000")
WITHDRAWN = Decimal("30000")
WITHDRAWAL_STATES = ("trainer_withdrawal_requested", "trainer_withdrawal_sent",
                     "trainer_withdrawal_refused")

#: agent_busy_day, one agent's day: the top-ups (to which of the three clients, how much)
#: before the operator's settlement and after it, the settlement, and the receivable
#: ceiling the operator set on THIS agent (its own, so the default ceiling is not read).
BUSY_BEFORE = ((0, Decimal("50000")), (1, Decimal("50000")))
BUSY_SETTLEMENT = Decimal("60000")
BUSY_AFTER = ((2, Decimal("50000")), (0, Decimal("45000")), (1, Decimal("45000")))
BUSY_CEILING = Decimal("200000")

#: client_wallet_short_of_coach: what the client's wallet holds, less than COACH_RATE.
SHORT_FUNDS = Decimal("10000")


def gross_for(earned) -> Decimal:
    """What a client sends a trainer so that `earned` is left after the platform's
    coaching commission (`COACHING_COMMISSION_RATE`), in whole units."""
    from django.conf import settings

    rate = Decimal(str(settings.COACHING_COMMISSION_RATE))
    return (Decimal(earned) / (1 - rate)).quantize(Decimal("1"), rounding=ROUND_UP)


def _what_a_withdrawal_state_moves(name):
    """The payer's cash at one new agent, and the trainer's one withdrawal that day."""
    return (agent_cash_gaps(name, in_a_day=gross_for(EARNED))
            + withdrawal_gaps(name, smallest=WITHDRAWN, in_a_day=WITHDRAWN))


for _name in WITHDRAWAL_STATES:
    needs(_name)(partial(_what_a_withdrawal_state_moves, _name))


@needs("agent_busy_day")
def _what_the_busy_agent_takes():
    """The day's top-ups at one new agent, against its daily and monthly caps. Its
    ceiling is the operator's own (`BUSY_CEILING`), not the default, so none is read."""
    day = sum(amount for _client, amount in BUSY_BEFORE + BUSY_AFTER)
    return agent_cash_gaps("agent_busy_day", in_a_day=day, unsettled=Decimal("0"))


@needs("client_wallet_short_of_coach")
def _what_the_short_clients_agent_takes():
    return agent_cash_gaps("client_wallet_short_of_coach", in_a_day=SHORT_FUNDS)


def call(walk, user, method: str, path: str, data=None, *, expect=(200, 201)):
    """`walk.api`, answering the body: the route as `user`'s app (the direct APK) calls
    it. A status outside `expect` raises and the state rolls back."""
    return walk.api(user, method, path, data, expect=expect)[1]


def key() -> str:
    return uuid.uuid4().hex


def money_of(user) -> str:
    """The currency `user` trades in (`wallet.currency_for`): the wallet's, or the
    platform's base currency for an account no money has reached yet."""
    from wallet import currency_for

    return currency_for(user)


# ------------------------------------------------------------------ the people
def operator(walk, part="operator"):
    """An operator (an `admin` account) for the console's own steps."""
    return walk.account(part, role="admin")


def agent(walk, part="agent", **console):
    """An active agent (`part=None`: the state's main account). `console` sets fields of
    the agent's profile as the operator's console does (`receivable_ceiling`, the caps)."""
    from wallet.models import ensure_agent_profile

    user = walk.account(part, role="agent")
    profile = ensure_agent_profile(user)
    profile.status = "active"
    for name, value in console.items():
        setattr(profile, name, Decimal(value))
    profile.save()
    return user


def funded_client(walk, part=None, *, amount, via=None, **fields):
    """A client whose wallet an agent topped up with `amount`, in the client's own
    currency (a new wallet's: pounds)."""
    client = walk.account(part, role="client", **fields)
    if Decimal(amount) > 0:
        top_up(walk, via or agent(walk, f"{part}_agent" if part else "agent"), client,
               amount)
    return client


def coach(walk, part="trainer", *, rate=COACH_RATE, **fields):
    """A trainer who charges `rate` pounds a month (`part=None`: the state's main
    account)."""
    return walk.account(part, role="trainer", trainer_hourly_rate=Decimal(rate), **fields)


def verified_coach(walk, the_operator, part="trainer", **fields):
    """A trainer verified the platform's way, through the verification service (the
    four documents sent, the application submitted) and the operator's approval
    (`trainer_verification.services.decide`, what the console calls)."""
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    from trainer_verification import services, slots

    trainer = coach(walk, part, **fields)
    for n, slot in enumerate(slots.REQUIRED):
        image = Image.new("RGB", (900, 600), ((n * 61) % 256, (n * 23) % 256, 120))
        for x in range(64):
            image.putpixel((x, (n * 13) % 600), (255, 255, 255))
        buf = io.BytesIO()
        image.save(buf, "JPEG")
        services.upload_document(trainer, slot, SimpleUploadedFile(
            f"{slot}.jpg", buf.getvalue(), content_type="image/jpeg"))
    application = services.submit(trainer)
    services.decide(application.pk, the_operator, approve=True)
    trainer.refresh_from_db()
    return trainer


def payout_method(walk, trainer, address="0a3f 91c2 7be4 55d0"):
    """A saved ShamCash payout method (`POST v1/trainer/payout-methods/`)."""
    return call(walk, trainer, "post", "/api/v1/trainer/payout-methods/", {
        "kind": "shamcash", "address": address, "address_confirm": address,
        "holder_name": f"{trainer.first_name} {trainer.last_name}"})


def earning_coach(walk, the_operator, *, amount, part=None):
    """A verified trainer with a payout method and `amount` pounds withdrawable now: a
    client of theirs, topped up by an agent, transferred it to them
    (`POST wallet/client/transfer/`, money the trainer may withdraw at once; the
    platform keeps its coaching commission, so the client sends a little more)."""
    trainer = verified_coach(walk, the_operator, part)
    payout_method(walk, trainer)
    gross = gross_for(amount)
    payer = funded_client(walk, f"{part}_client" if part else "client", amount=gross,
                          first_name="رامي", last_name="الحلبي")
    call(walk, payer, "post", "/api/wallet/client/transfer/", {
        "trainer_id": trainer.pk, "amount": f"{gross:.2f}", "idempotency_key": key()})
    return trainer


def withdraw(walk, trainer, amount):
    """The trainer asks for a withdrawal (`POST v1/trainer/withdrawals/`)."""
    return call(walk, trainer, "post", "/api/v1/trainer/withdrawals/", {
        "amount": f"{Decimal(amount):.2f}", "idempotency_key": key()})


# ------------------------------------------------------------------ 4. withdrawals
@on_retire
def _close_the_withdrawals_of_retired_walk_trainers(users):
    """A withdrawal waits on the operator's desk (the console's queue, the operator
    phone's `withdrawals` count) whoever asked for it, so every run of the withdrawal
    states would leave two more open under retired trainers. Each open one (requested,
    approved or sent) is closed as an operator closes one that cannot be paid, through
    `payouts.services.fail` with its reason: the money goes back to the retired
    trainer's balance, its events are written as for any failure, and the queue is as it
    was. The state's own operator (its `operator` part) is the one who closes it. Walk
    data only: `users` are walk accounts being retired."""
    from payouts import services
    from payouts.models import OPEN_STATES, Withdrawal

    the_operator = next((user for user in users if user.user_type == "admin"), None)
    still_open = (Withdrawal.objects.filter(trainer__in=users, state__in=OPEN_STATES)
                  .order_by("pk").values_list("pk", flat=True))
    for withdrawal_id in list(still_open):
        services.fail(withdrawal_id, the_operator,
                      "Walk-through account reset: this request was closed.")


@state("trainer_withdrawal_requested", area="wallet", role="trainer",
       summary="A verified trainer with a payout method, whose withdrawal waits for the "
               "operator.")
def trainer_withdrawal_requested(walk):
    the_operator = operator(walk)
    trainer = earning_coach(walk, the_operator, amount=EARNED)
    made = withdraw(walk, trainer, WITHDRAWN)
    walk.ids(withdrawal=made["id"], operator=the_operator.pk)


@state("trainer_withdrawal_sent", area="wallet", role="trainer",
       summary="A trainer whose withdrawal the operator approved and sent; it has not "
               "been confirmed as arrived.")
def trainer_withdrawal_sent(walk):
    from payouts import services

    the_operator = operator(walk)
    trainer = earning_coach(walk, the_operator, amount=EARNED)
    made = withdraw(walk, trainer, WITHDRAWN)
    services.approve(made["id"], the_operator)
    services.mark_sent(made["id"], the_operator, transaction_id=f"WALK-{key()[:12]}")
    walk.ids(withdrawal=made["id"], operator=the_operator.pk)


@state("trainer_withdrawal_refused", area="wallet", role="trainer",
       summary="A trainer whose withdrawal the operator could not pay: refused with a "
               "reason, the money back in the balance.")
def trainer_withdrawal_refused(walk):
    from payouts import services

    the_operator = operator(walk)
    trainer = earning_coach(walk, the_operator, amount=EARNED)
    made = withdraw(walk, trainer, WITHDRAWN)
    services.fail(made["id"], the_operator,
                  "رقم حساب شام كاش لا يطابق اسم صاحبه. صحّح طريقة الدفع ثم اطلب السحب من جديد.")
    walk.ids(withdrawal=made["id"], operator=the_operator.pk)


# ------------------------------------------------------------------ 5. agents
@state("agent_busy_day", area="wallet", role="agent",
       summary="An agent with today's top-ups, an operator's settlement, and the cash "
               "held for the platform near the ceiling.")
def agent_busy_day(walk):
    from wallet import agents
    from wallet.models import AgentProfile, ensure_agent_profile

    the_operator = operator(walk)
    clients = [walk.account(f"client{n}", role="client", first_name=name, last_name=family)
               for n, (name, family) in enumerate(
                   [("ليلى", "خوري"), ("مازن", "العلي"), ("هبة", "سعيد")], start=1)]
    # The cash the clients hand over is in their currency; the ceiling the operator set
    # and the settlement are in that one (`AgentProfile.CEILING_FIELDS`).
    currency = money_of(clients[0])
    me = agent(walk, None, **{AgentProfile.CEILING_FIELDS[currency]: BUSY_CEILING})
    for client, amount in BUSY_BEFORE:
        top_up(walk, me, clients[client], amount)
    profile = ensure_agent_profile(me)
    settlement = agents.record_settlement(profile.pk, currency, BUSY_SETTLEMENT,
                                          the_operator, note="تسليم نقدي في المكتب",
                                          key=key())
    for client, amount in BUSY_AFTER:
        top_up(walk, me, clients[client], amount)
    walk.ids(clients=[c.pk for c in clients], settlement=settlement.pk,
             operator=the_operator.pk, currency=currency)


@state("agent_key_paired_elsewhere", area="wallet", role="agent",
       summary="An agent whose signing key is paired on another phone: this phone is "
               "told so and tops up nothing until the key is moved.")
def agent_key_paired_elsewhere(walk):
    me = agent(walk, None)
    paired = call(walk, me, "post", "/api/wallet/agent/apikey/ensure/",
                  {"device_name": "Samsung A54"})
    walk.ids(key=paired["key_id"], paired_device="Samsung A54")


# ------------------------------------------------------------------ 8. 9. client wallets
@state("client_wallet_short_of_coach", area="wallet", role="client",
       summary="A client whose wallet holds less than a trainer's month: asking that "
               "trainer is refused for the balance.")
def client_wallet_short_of_coach(walk):
    trainer = coach(walk)
    funded_client(walk, amount=SHORT_FUNDS)
    walk.ids(trainer=trainer.pk, rate=f"{COACH_RATE:.2f}")


@state("client_wallet_untouched", area="wallet", role="client",
       summary="A new client whose wallet was never topped up: nothing in it and no "
               "currency chosen yet.")
def client_wallet_untouched(walk):
    walk.account(role="client")
