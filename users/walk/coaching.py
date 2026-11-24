"""Walk states: coaching and plans (`users/walk/__init__.py` says how to add one).

Owned by the money worker. Every state goes through the platform's real paths: the
routes the app calls (`walk.api`, through `users/walk/wallet.py call`: the request and
the answers, `users/walk/shared.py coach_client`, the first programme, a plan paid from
the wallet, the renewal switch, a cancel), and the scheduled task's own function for
what time does. A state that time makes moves the DATES (a coaching month's or a plan's
start and end, nothing else) and then calls the scheduled work FOR ITS OWN ROWS ONLY:
`users.coaching.renew(relation_id)` (the hourly run's renewal or freeze of one
relation), `users.coaching.remind_upcoming(relation_ids=...)`,
`subscription.tasks.expire_lapsed_subscriptions(subscription_ids=...)` and
`send_renewal_reminders(subscription_ids=...)`. Never the whole sweep: on a shared
development database that renewed, froze, lapsed and reminded every builder's own
accounts (the framework owner's review, 2026-11-24). Never a hand-written ledger row,
payment state or hold.

**No state needs an exchange rate.** A plan is chosen in the client's OWN currency
(`shared.plan_for(client)`: the cheapest on sale in that currency, the one rule every
walk area uses) and bought in it, so nothing is converted; prices in two currencies are
never compared (a dollar plan at 12.00 is not "cheaper" than a pound plan at 1,000).
Where no paid plan is on sale in the client's currency the state refuses and says so: it
never makes or prices a plan, never makes the wallet follow the plan's currency, and
never sets a rate (the operator's value; a development database may have none). The
coaching price is in the trainer's currency, which is the client's (both are fresh
accounts: `users/walk/wallet.py`).

The states that read platform data (a plan on sale in the currency a new wallet takes,
the diet templates and the food catalogue, the starter exercises) and the settings they
move money under (a new agent's caps, the payout minimum and daily cap) are checked once,
before any state writes anything, and every gap is named in one refusal
(`framework.refuse_unless_ready`: `bootstrap_gaps`, and the `@needs` the framework
declares for these states).

Not reachable here, by design of the platform rather than of this command:
- **a plan payment pending review** needs a checkout opened on the ShamCash gateway and
  the gateway's own account read seeing money for it (`subscription/tasks.py`
  `poll_open_payments` -> `PaymentService.hold_for_review`). A development server has no
  ShamCash account configured (`SHAMCASH_*` unset), so no checkout opens and nothing can
  see a transfer; it is made only where the gateway is configured, never by writing the
  status.
- **a request WAITING with its fee held**: the platform holds a coaching fee when the
  trainer ACCEPTS (`approve_trainer_relation` -> `take_hold`), never while a request
  waits. `client_request_waiting` is a waiting request (nothing held, the quote shown);
  `client_awaiting_first_program` is the held month after acceptance.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from functools import partial

from django.utils import timezone

from . import state
from .framework import WalkRefused, agent_cash_gaps, needs, on_retire
from .shared import coach_client, plan_for, starter_exercise, top_up
from .wallet import COACH_RATE, agent, call, coach, funded_client, key, money_of

# ------------------------------------------------------------------ what the states move
#: What a walk client is handed beyond what they pay: a little, never a second month.
SPARE = Decimal("5000")

#: The cash each state's client is handed at the state's ONE new agent, on one day,
#: beside the price of the plan it buys where it buys one (`PLAN`). Read by each state
#: AND by its `@needs` (`_cash_gaps`), so the check before anything is written cannot
#: drift from what the state tops up. In a new wallet's currency.
CASH = {
    "coaching_frozen": COACH_RATE + SPARE,           # + the plan: one month, not a second
    "coaching_renews_soon": COACH_RATE * 2 + SPARE,  # the next month covered
    "coaching_stops_soon": COACH_RATE * 2 + SPARE,
    "plan_lapsed": Decimal("0"),
    "plan_not_renewing": Decimal("0"),
    "plan_renewal_due": Decimal("0"),
    "client_request_waiting": COACH_RATE + SPARE,
    "client_awaiting_first_program": COACH_RATE + SPARE,
    "client_invited_by_coach": COACH_RATE + SPARE,
    "client_wallet_covers_plan": Decimal("0"),        # the plan's price exactly
}

#: The states that buy (or price) a plan, and whether it has the diet: `plan_for` in the
#: state, `shared.choose_plan` (the same choice, for a new client) in the check.
PLAN = {"coaching_frozen": True, "plan_lapsed": True, "plan_not_renewing": False,
        "plan_renewal_due": False, "client_wallet_covers_plan": False}


def _cash_gaps(name):
    """One new agent takes the state's `CASH`, and the price of its plan, in a day and
    holds it unsettled. With no plan on sale for it, the framework's own plan need names
    that gap, so none is added here."""
    from .shared import choose_plan, new_wallet_currency

    total = CASH[name]
    if name in PLAN:
        plan = choose_plan(new_wallet_currency(), diet=PLAN[name])
        if plan is None:
            return []
        total += plan.price
    return agent_cash_gaps(name, in_a_day=total)


for _name in CASH:
    needs(_name)(partial(_cash_gaps, _name))


# ------------------------------------------------------------------ on a reset
@on_retire
def _return_the_held_months_of_retired_walk_clients(users):
    """A month held for a programme never sent (`client_awaiting_first_program`) stays in
    escrow when its client is retired, a little more of it every run. It is given back
    the way an erasure gives it back (`users.trainer_relations.release_hold`: nothing
    was delivered for it, so it returns to the client), under the relation's lock. A
    paid-out month (every other money state's) holds nothing and is left alone. Walk
    data only: `users` are walk accounts being retired."""
    from users.models import TrainerClientRelation
    from users.trainer_relations import release_hold

    held = (TrainerClientRelation.objects.filter(client__in=users)
            .exclude(hold_reference="").select_related("client").order_by("pk"))
    for relation in held:
        release_hold(relation, relation.client)


# ------------------------------------------------------------------ coaching steps
def ask_for(walk, client, trainer):
    """The client asks the trainer to coach them (`POST client/request-trainer/`)."""
    from users.models import TrainerClientRelation

    call(walk, client, "post", "/api/auth/client/request-trainer/", {"trainer_id": trainer.pk})
    return TrainerClientRelation.objects.get(trainer=trainer, client=client)


def first_programme(walk, trainer, client, name="برنامج الشهر الأول"):
    """The trainer writes a one-day programme of a starter exercise and sends it to the
    client (`assign_to_client`): the held month is paid out."""
    from routine.models import Routine, RoutineExercise

    routine = Routine.objects.create(name=name, created_by=trainer, days=1)
    RoutineExercise.objects.create(routine=routine, exercise=starter_exercise(walk),
                                   day=1, sets=3, reps=10)
    call(walk, trainer, "post", f"/api/routine/routines/{routine.pk}/assign_to_client/",
         {"client_id": client.pk})
    return routine


def coached(walk, *, funds, rate=COACH_RATE, deliver=True):
    """A client (the state's main account) topped up with `funds`, coached by a trainer
    charging `rate` (`coach_client`: month 1 held); with `deliver`, the first programme
    is sent and month 1 paid out."""
    trainer = coach(walk, rate=rate)
    client = funded_client(walk, amount=funds)
    relation = coach_client(walk, client, trainer)
    routine = first_programme(walk, trainer, client) if deliver else None
    return trainer, client, relation, routine


def month_ends_at(relation, when):
    """Move the current month so it ends at `when` (its length kept): the dates only."""
    period = relation.periods.order_by("-number").first()
    shift = period.ends_at - when
    type(period).objects.filter(pk=period.pk).update(
        starts_at=period.starts_at - shift, ends_at=period.ends_at - shift)
    return period


def renew_now(relation):
    """What the hourly run does for THIS relation at its month's end: renew it, or
    freeze it when the wallet cannot cover the month (`users.coaching.renew`)."""
    from users.coaching import renew

    return renew(relation.pk)


def remind_now(relation):
    """The hourly run's 3-day reminder, for THIS relation only."""
    from users.coaching import remind_upcoming

    return remind_upcoming(relation_ids=[relation.pk])


def lapse_now(subscription):
    """The daily run's lapse of an ended plan, for THIS subscription only."""
    from subscription.tasks import expire_lapsed_subscriptions

    return expire_lapsed_subscriptions(subscription_ids=[subscription.pk])


def plan_reminders_now(subscription):
    """The daily run's plan reminders (ending soon, then lapsed), for THIS
    subscription only."""
    from subscription.tasks import send_renewal_reminders

    return send_renewal_reminders(subscription_ids=[subscription.pk])


# ------------------------------------------------------------------ plan steps
def plan_price(plan, client):
    """The plan's price as the plan list quotes it to the client: the price of record,
    the plan being in the client's own currency (`shared.plan_for`), so no rate is read.
    A plan in another currency is refused, never converted."""
    from subscription.pricing import charge_for

    currency = money_of(client)
    if plan.currency != currency:
        raise WalkRefused(f"plan {plan.pk} is priced in {plan.currency}, the walk client "
                          f"trades in {currency}: the walk states never convert a price")
    return charge_for(plan.price, plan.currency, currency)


def buy_plan(walk, client, plan):
    """The client chooses the plan (`POST v1/subscriptions/`) and pays it from the
    wallet (`pay-from-wallet/`, the quote the screen showed)."""
    from subscription.models import Subscription

    made = call(walk, client, "post", "/api/subscription/v1/subscriptions/",
                {"plan_id": plan.pk})
    priced = plan_price(plan, client)
    call(walk, client, "post",
         f"/api/subscription/v1/subscriptions/{made['id']}/pay-from-wallet/",
         {"idempotency_key": key(), "amount": f"{priced.amount:.2f}",
          "currency": priced.currency})
    return Subscription.objects.get(pk=made["id"])


def plan_ends_at(subscription, when):
    """Move the plan's cover so it ends at `when` (its length kept): the dates only."""
    from subscription.models import Subscription

    subscription.refresh_from_db()
    shift = subscription.end_date - when
    Subscription.objects.filter(pk=subscription.pk).update(
        start_date=subscription.start_date - shift, end_date=subscription.end_date - shift)


def subscribed_client(walk, name):
    """A client (main account) of state `name` who bought the cheapest plan on sale in
    their own currency (`shared.plan_for`, with the diet as `PLAN` says) from a wallet an
    agent topped up with its price and the state's `CASH`."""
    client = walk.account(role="client")
    plan = plan_for(client, diet=PLAN[name])
    top_up(walk, agent(walk), client, plan_price(plan, client).amount + CASH[name])
    return client, buy_plan(walk, client, plan)


def coach_diet_plan(trainer, client):
    """The trainer writes the client a one-week diet plan with one lunch today
    (`TrainerDietPlanService`: a draft, sent by its first meal)."""
    from diet.models import DietPlanTemplate, FoodItem
    from diet.trainer_services import TrainerDietPlanService

    template = DietPlanTemplate.objects.order_by("id").first()
    foods = list(FoodItem.objects.filter(needs_review=False).order_by("id")[:2])
    service = TrainerDietPlanService(trainer)
    today = timezone.localdate()
    plan = service.create_diet_plan(client, template, today, duration_weeks=1)
    service.add_meal_to_plan(plan, "Lunch", today,
                             [{"food_id": food.pk, "quantity": 150} for food in foods])
    plan.refresh_from_db()
    return plan


# ------------------------------------------------------------------ 1. frozen
@state("coaching_frozen", area="coaching", role="client",
       summary="A coaching whose month ended unpaid, and a plan that lapsed: the coach's "
               "routine and diet plan are readable and cannot be used.")
def coaching_frozen(walk):
    trainer = coach(walk)
    client = walk.account(role="client")
    plan = plan_for(client, diet=PLAN["coaching_frozen"])
    # The plan's price and one month of coaching, and a little: not a second month.
    top_up(walk, agent(walk), client,
           plan_price(plan, client).amount + CASH["coaching_frozen"])
    subscription = buy_plan(walk, client, plan)
    relation = coach_client(walk, client, trainer)
    routine = first_programme(walk, trainer, client)
    diet_plan = coach_diet_plan(trainer, client)
    # A month later: the coaching month ends unpaid (the wallet holds 5,000 of 40,000)
    # and the hourly run freezes it; the plan's cover ends and the daily run lapses it.
    month_ends_at(relation, timezone.now() - timedelta(hours=2))
    outcome = renew_now(relation)
    if outcome != "frozen":
        raise WalkRefused(f"coaching_frozen: the month's end answered {outcome!r}, "
                          f"not 'frozen'")
    plan_ends_at(subscription, timezone.now() - timedelta(hours=2))
    lapse_now(subscription)
    walk.ids(trainer=trainer.pk, relation=relation.pk, routine=routine.pk,
             diet_plan=diet_plan.pk, subscription=str(subscription.pk), plan=plan.pk)


# ------------------------------------------------------------------ 2. ending soon
@state("coaching_renews_soon", area="coaching", role="client",
       summary="A paid coaching month ending in under 3 days; the wallet covers the "
               "next one and the renewal reminder has gone out.")
def coaching_renews_soon(walk):
    trainer, client, relation, routine = coached(walk, funds=CASH["coaching_renews_soon"])
    month_ends_at(relation, timezone.now() + timedelta(days=2, hours=20))
    remind_now(relation)
    walk.ids(trainer=trainer.pk, relation=relation.pk, routine=routine.pk)


@state("coaching_stops_soon", area="coaching", role="client",
       summary="A paid coaching month ending in under 3 days that the client asked not "
               "to renew (stop_renewal on); the ending reminder has gone out.")
def coaching_stops_soon(walk):
    trainer, client, relation, routine = coached(walk, funds=CASH["coaching_stops_soon"])
    call(walk, client, "post", "/api/v1/coaching/renewal/", {"renew": False})
    month_ends_at(relation, timezone.now() + timedelta(days=2, hours=20))
    remind_now(relation)
    walk.ids(trainer=trainer.pk, relation=relation.pk, routine=routine.pk)


# ------------------------------------------------------------------ 3. plans
@state("plan_lapsed", area="coaching", role="client",
       summary="A plan paid from the wallet whose cover ended: frozen (the diet plans "
               "readable, nothing usable) and asked to re-subscribe.")
def plan_lapsed(walk):
    client, subscription = subscribed_client(walk, "plan_lapsed")
    plan_ends_at(subscription, timezone.now() - timedelta(days=1, hours=2))
    lapse_now(subscription)
    plan_reminders_now(subscription)
    walk.ids(subscription=str(subscription.pk), plan=subscription.plan_id)


@state("plan_not_renewing", area="coaching", role="client",
       summary="An active plan the client turned off renewing: it runs to its end date.")
def plan_not_renewing(walk):
    client, subscription = subscribed_client(walk, "plan_not_renewing")
    call(walk, client, "post",
         f"/api/subscription/v1/subscriptions/{subscription.pk}/cancel/", {})
    walk.ids(subscription=str(subscription.pk), plan=subscription.plan_id)


@state("plan_renewal_due", area="coaching", role="client",
       summary="An active plan ending within 3 days: renewal_due, and the reminder sent.")
def plan_renewal_due(walk):
    client, subscription = subscribed_client(walk, "plan_renewal_due")
    plan_ends_at(subscription, timezone.now() + timedelta(days=2, hours=12))
    plan_reminders_now(subscription)
    walk.ids(subscription=str(subscription.pk), plan=subscription.plan_id)


# ------------------------------------------------------------------ 6. before a coaching
@state("client_request_waiting", area="coaching", role="client",
       summary="A client whose request to a trainer is waiting for the trainer's answer "
               "(the quote shown; the fee is held only once the trainer accepts).")
def client_request_waiting(walk):
    trainer = coach(walk)
    client = funded_client(walk, amount=CASH["client_request_waiting"])
    relation = ask_for(walk, client, trainer)
    walk.ids(trainer=trainer.pk, request=relation.pk)


@state("client_awaiting_first_program", area="coaching", role="client",
       summary="A client the trainer accepted: month 1 held, the first programme not "
               "sent yet.")
def client_awaiting_first_program(walk):
    trainer, client, relation, _routine = coached(
        walk, funds=CASH["client_awaiting_first_program"], deliver=False)
    walk.ids(trainer=trainer.pk, relation=relation.pk)


@state("client_invited_by_coach", area="coaching", role="client",
       summary="A client a trainer invited, at the trainer's price; the wallet covers it.")
def client_invited_by_coach(walk):
    from users.models import TrainerClientRelation

    trainer = coach(walk)
    client = funded_client(walk, amount=CASH["client_invited_by_coach"])
    call(walk, trainer, "post", "/api/auth/trainer/assign-client/",
         {"client_id": client.pk, "message": "أهلاً! أنا مدرّبك المقترح لبرنامج القوة."})
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    walk.ids(trainer=trainer.pk, request=relation.pk)


# ------------------------------------------------------------------ 7. a plan in reach
@state("client_wallet_covers_plan", area="coaching", role="client",
       summary="A client with no plan whose wallet covers the cheapest plan in its "
               "currency exactly.")
def client_wallet_covers_plan(walk):
    client = walk.account(role="client")
    plan = plan_for(client, diet=PLAN["client_wallet_covers_plan"])
    top_up(walk, agent(walk), client,
           plan_price(plan, client).amount + CASH["client_wallet_covers_plan"])
    walk.ids(plan=plan.pk)
