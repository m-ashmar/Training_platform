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
that currency and the agent's caps, ceiling and settlement are read in it; a trainer is
a fresh account too, so the coaching price (`COACH_RATE`, in the trainer's currency) and
the client's money are the same currency and a transfer never crosses. Nothing here
converts, and nothing creates or sets an exchange rate: that is the operator's value,
and a development database may have none (the app builders' does, on purpose).

The helpers (`call`, `operator`, `agent`, `top_up`, `funded_client`, `coach`,
`money_of`) are shared with `users/walk/coaching.py`, the other money module.
"""
from __future__ import annotations

import uuid
from decimal import ROUND_UP, Decimal

from . import state
from .framework import on_retire

#: A trainer's monthly fee in the walk states, in the trainer's currency (a fresh
#: account's: the platform's base currency, pounds).
COACH_RATE = Decimal("40000.00")


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


def top_up(walk, agent_user, client, amount, currency=None):
    """Cash handed to `agent_user`, credited to `client` by the agent's app
    (`POST wallet/agent/topup/proxy/`, WALLET_MOVE), in the client's own currency
    (`money_of`) unless one is named. An amount above the agent's per-top-up cap in
    that currency is handed over in several top-ups, as an agent would."""
    from wallet.models import ensure_agent_profile

    currency = currency or money_of(client)
    amount = Decimal(amount)
    cap = ensure_agent_profile(agent_user).per_transaction_limit_for(currency) or amount
    receipts = []
    while amount > 0:
        part = min(amount, Decimal(cap))
        receipts.append(call(walk, agent_user, "post", "/api/wallet/agent/topup/proxy/", {
            "client_identifier": str(client.pk), "amount": f"{part:.2f}",
            "currency": currency, "idempotency_key": key()}))
        amount -= part
    return receipts


def funded_client(walk, part=None, *, amount, via=None, **fields):
    """A client whose wallet an agent topped up with `amount` pounds."""
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
    from django.conf import settings

    trainer = verified_coach(walk, the_operator, part)
    payout_method(walk, trainer)
    rate = Decimal(str(settings.COACHING_COMMISSION_RATE))
    gross = (Decimal(amount) / (1 - rate)).quantize(Decimal("1"), rounding=ROUND_UP)
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
    trainer = earning_coach(walk, the_operator, amount="60000")
    made = withdraw(walk, trainer, "30000")
    walk.ids(withdrawal=made["id"], operator=the_operator.pk)


@state("trainer_withdrawal_sent", area="wallet", role="trainer",
       summary="A trainer whose withdrawal the operator approved and sent; it has not "
               "been confirmed as arrived.")
def trainer_withdrawal_sent(walk):
    from payouts import services

    the_operator = operator(walk)
    trainer = earning_coach(walk, the_operator, amount="60000")
    made = withdraw(walk, trainer, "30000")
    services.approve(made["id"], the_operator)
    services.mark_sent(made["id"], the_operator, transaction_id=f"WALK-{key()[:12]}")
    walk.ids(withdrawal=made["id"], operator=the_operator.pk)


@state("trainer_withdrawal_refused", area="wallet", role="trainer",
       summary="A trainer whose withdrawal the operator could not pay: refused with a "
               "reason, the money back in the balance.")
def trainer_withdrawal_refused(walk):
    from payouts import services

    the_operator = operator(walk)
    trainer = earning_coach(walk, the_operator, amount="60000")
    made = withdraw(walk, trainer, "30000")
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
    me = agent(walk, None, **{AgentProfile.CEILING_FIELDS[currency]: "200000"})
    top_up(walk, me, clients[0], "50000")
    top_up(walk, me, clients[1], "50000")
    profile = ensure_agent_profile(me)
    settlement = agents.record_settlement(profile.pk, currency, Decimal("60000"),
                                          the_operator, note="تسليم نقدي في المكتب",
                                          key=key())
    top_up(walk, me, clients[2], "50000")
    top_up(walk, me, clients[0], "45000")
    top_up(walk, me, clients[1], "45000")
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
    funded_client(walk, amount="10000")
    walk.ids(trainer=trainer.pk, rate=f"{COACH_RATE:.2f}")


@state("client_wallet_untouched", area="wallet", role="client",
       summary="A new client whose wallet was never topped up: nothing in it and no "
               "currency chosen yet.")
def client_wallet_untouched(walk):
    walk.account(role="client")
