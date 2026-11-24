"""Walk states: inbox (`users/walk/__init__.py` says how to add one).

Owned by the phase 3 worker.
"""
import re
from decimal import ROUND_UP, Decimal

from . import state
from .framework import agent_cash_gaps, needs, withdrawal_gaps

#: A value for every placeholder a notice template names, so each renders as it reads
#: in the app (`notifications/domain/*.py`; the resolver answers a fallback sentence for
#: a notice whose context lacks one).
SAMPLES = {
    "actor": "رنا", "name": "هادي", "points": 25, "title": "تحدي الأيام", "progress": 3,
    "unit": "أيام", "sender": "هادي", "preview": "كيف كان تمرين اليوم؟", "days": 3,
    "month": 2, "message": "أحسنت، استمر على هذا الإيقاع!", "exercise": "ضغط بالبار",
    "weight": 60, "reps": 8, "agent": "وكيل المزة", "number": "W-1001",
    "reason": "عنوان الاستلام غير صحيح", "trained": 3, "clients": 4, "attention": 1,
    "logged": 18, "planned": 24, "perfect": 2,
}

PLACEHOLDER = re.compile(r"%\((\w+)\)s")

#: The people ids a notice's `data` carries (API_CONTRACT §4), filled with the walk's own
#: two accounts; every other notice carries `type` alone (what it names is not made).
PEOPLE_KEYS = {
    "user_followed": ("follower_id",),
    "trainer_assignment_request": ("trainer_id",),
    "trainer_unassignment": ("trainer_id",),
    "client_request_received": ("client_id",),
    "client_request_approved": ("trainer_id", "client_id"),
    "client_request_rejected": ("trainer_id",),
    "client_request_cancelled": ("client_id",),
    "client_unassigned_trainer": ("client_id",),
    "coaching_ended": ("user_id", "client_id", "trainer_id"),
    "coaching_finished": ("user_id", "client_id", "trainer_id"),
}


#: The trainer kinds the state makes for real rather than writing them: a notice of each
#: owes the trainer an e-mail (`notifications/channels/email.py` NOTIFICATION_EMAILS),
#: whose composer reads the withdrawal it names. A written one would name none, so its
#: e-mail would fail and be reported. The payout steps send them (`payouts/services.py`).
MADE_FOR_REAL = ("withdrawal_requested", "withdrawal_paid", "withdrawal_failed")

#: What the coach earns before the two withdrawals, in a new wallet's currency, and what
#: each takes out (the one paid, the one that fails), both on the same day. Read by the
#: state and by its `@needs`, so the check cannot drift from what the state moves.
EARNED = Decimal("40000")
PAID_OUT, FAILED = Decimal("20000"), Decimal("15000")


def _gross() -> Decimal:
    """What the client sends the coach so that EARNED is left after the platform's
    coaching commission: the cash one agent takes from that client, in one top-up day."""
    from django.conf import settings

    rate = Decimal(str(settings.COACHING_COMMISSION_RATE))
    return (EARNED / (1 - rate)).quantize(Decimal("1"), rounding=ROUND_UP)


@needs("inbox_every_kind")
def _what_the_inbox_coach_moves():
    return (agent_cash_gaps("inbox_every_kind", in_a_day=_gross())
            + withdrawal_gaps("inbox_every_kind", smallest=min(PAID_OUT, FAILED),
                              in_a_day=PAID_OUT + FAILED))


def _withdrawals(walk, coach):
    """The coach's two withdrawals, each through the routes and services a real one goes
    through: verified, a ShamCash payout method, money a client sent them (topped up in
    cash at an agent, the platform keeping its commission), then one withdrawal the
    operator approved, sent and confirmed (`withdrawal_requested`, `withdrawal_paid`)
    and one the operator could not pay (`withdrawal_requested`, `withdrawal_failed`).
    Neither is left open, so nothing waits on the operator's desk. Answers the ids."""
    import uuid

    from payouts import services

    from .shared import open_agent, top_up, verify_trainer

    def key():
        return f"walk-{uuid.uuid4().hex[:24]}"

    operator = walk.account("operator", role="admin")
    verify_trainer(walk, coach, operator)
    address = "0a3f 91c2 7be4 55d0"
    walk.api(coach, "post", "/api/v1/trainer/payout-methods/", {
        "kind": "shamcash", "address": address, "address_confirm": address,
        "holder_name": f"{coach.first_name} {coach.last_name}"})

    gross = _gross()
    agent = open_agent(walk)
    payer = walk.account("payer", first_name="رامي", last_name="الحلبي")
    top_up(walk, agent, payer, f"{gross:.2f}")
    walk.api(payer, "post", "/api/wallet/client/transfer/",
             {"trainer_id": coach.pk, "amount": f"{gross:.2f}", "idempotency_key": key()})

    def withdraw(amount):
        _s, made = walk.api(coach, "post", "/api/v1/trainer/withdrawals/",
                            {"amount": f"{amount:.2f}", "idempotency_key": key()})
        return made["id"]

    paid = withdraw(PAID_OUT)
    services.approve(paid, operator)
    services.mark_sent(paid, operator, transaction_id=f"WALK-{uuid.uuid4().hex[:12]}")
    services.confirm(paid, operator)
    failed = withdraw(FAILED)
    services.fail(failed, operator, SAMPLES["reason"])
    return {"operator": operator.pk, "agent": agent.pk, "payer": payer.pk,
            "withdrawal_paid": paid, "withdrawal_failed": failed}


def _context(template):
    names = set()
    for text in (str(template.title), str(template.body)) if template else ():
        names.update(PLACEHOLDER.findall(text))
    missing = names - set(SAMPLES)
    if missing:
        raise ValueError(f"inbox walk: no sample for {sorted(missing)}")
    return {name: SAMPLES[name] for name in names}


def kinds_for(role):
    """The notice kinds a role receives (`notifications/audience.py`), sorted."""
    from notifications.audience import AUDIENCE
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    return sorted(kind for kind in EVENT_CLASS_REGISTRY if role in AUDIENCE.get(kind, ()))


@state("inbox_every_kind", area="inbox", role="client",
       summary="An inbox holding one notice of every kind a client receives, and the "
               "trainer part's of every kind a trainer receives (the three build-gated "
               "ones included; the withdrawal ones from two real withdrawals, one paid "
               "and one refused).")
def inbox_every_kind(walk):
    import uuid

    from notifications.channels.fcm import _resolve_event_template
    from notifications.services import NotificationService

    me = walk.account()
    coach = walk.account("coach", role="trainer")
    made = _withdrawals(walk, coach)
    people = {"client": (me, coach), "trainer": (coach, me)}
    written = {}
    for role, (recipient, counterpart) in people.items():
        for kind in kinds_for(role):
            if role == "trainer" and kind in MADE_FOR_REAL:
                written.setdefault(role, []).append(kind)
                continue
            # The ids a tap opens with, where they are people: the other one of the two.
            data = {"type": kind}
            for key in PEOPLE_KEYS.get(kind, ()):
                data[key] = (me.pk if key == "client_id" else
                             coach.pk if key == "trainer_id" else counterpart.pk)
            NotificationService.create_and_send(
                recipient=recipient, event_type=kind, related_object_id=uuid.uuid4().hex,
                metadata={"context": _context(_resolve_event_template(kind)),
                          "data": data})
            written.setdefault(role, []).append(kind)
    walk.ids(coach=coach.pk, client_kinds=written["client"],
             trainer_kinds=written["trainer"], **made)
