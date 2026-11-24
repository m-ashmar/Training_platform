"""Walk states: coaching and plans (`users/walk/__init__.py` says how to add one).

Owned by the money worker. Every state goes through the platform's real paths: the
routes the app calls (`walk.api`, through `users/walk/wallet.py call`: the request and
the answers, `users/walk/shared.py coach_client`, the first programme, a plan paid from
the wallet, the renewal switch, a cancel), and the scheduled task's own function for
what time does. A state that time makes moves the DATES (a coaching month's or a plan's
start and end, nothing else) and then calls that task:
`users.tasks.renew_coaching_periods` (reminders, renewals, freezes),
`subscription.tasks.expire_lapsed_subscriptions`,
`subscription.tasks.send_renewal_reminders`. Never a hand-written ledger row, payment
state or hold. The states that read platform data (a plan on sale, the diet templates
and the food catalogue, the starter exercises) need a bootstrapped database: the
command refuses once, before any state, when one is not (`framework.bootstrap_gaps`).

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

from django.utils import timezone

from . import state
from .shared import coach_client, starter_exercise
from .wallet import COACH_RATE, agent, call, coach, funded_client, key, top_up


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


def run_the_coaching_hour():
    """The hourly beat entry: reminders, then renewals and freezes."""
    from users.tasks import renew_coaching_periods

    renew_coaching_periods()


# ------------------------------------------------------------------ plan steps
def cheapest_plan(*, diet=False):
    """The cheapest paid plan on sale (with the diet: `framework.bootstrap_gaps` makes
    sure there is one)."""
    from subscription.models import SubscriptionPlan

    plans = SubscriptionPlan.objects.filter(is_active=True, price__gt=0)
    if diet:
        plans = plans.filter(has_diet_access=True)
    return plans.order_by("price", "id").first()


def plan_price(plan, client):
    """The plan's price in the client's currency, as the plan list quotes it."""
    from subscription.pricing import charge_for
    from wallet import currency_for

    return charge_for(plan.price, plan.currency, currency_for(client))


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


def subscribed_client(walk, *, diet=False, extra=Decimal("0")):
    """A client (main account) who bought the cheapest plan from a wallet an agent
    topped up with its price (and `extra` more)."""
    plan = cheapest_plan(diet=diet)
    client = walk.account(role="client")
    top_up(walk, agent(walk), client, plan_price(plan, client).amount + Decimal(extra))
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
    from subscription.tasks import expire_lapsed_subscriptions

    plan = cheapest_plan(diet=True)
    trainer = coach(walk)
    client = walk.account(role="client")
    # The plan's price and one month of coaching, and a little: not a second month.
    top_up(walk, agent(walk), client, plan_price(plan, client).amount + COACH_RATE + 5000)
    subscription = buy_plan(walk, client, plan)
    relation = coach_client(walk, client, trainer)
    routine = first_programme(walk, trainer, client)
    diet_plan = coach_diet_plan(trainer, client)
    # A month later: the coaching month ends unpaid (the wallet holds 5,000 of 40,000)
    # and the hourly run freezes it; the plan's cover ends and the daily run lapses it.
    month_ends_at(relation, timezone.now() - timedelta(hours=2))
    run_the_coaching_hour()
    plan_ends_at(subscription, timezone.now() - timedelta(hours=2))
    expire_lapsed_subscriptions()
    walk.ids(trainer=trainer.pk, relation=relation.pk, routine=routine.pk,
             diet_plan=diet_plan.pk, subscription=str(subscription.pk), plan=plan.pk)


# ------------------------------------------------------------------ 2. ending soon
@state("coaching_renews_soon", area="coaching", role="client",
       summary="A paid coaching month ending in under 3 days; the wallet covers the "
               "next one and the renewal reminder has gone out.")
def coaching_renews_soon(walk):
    trainer, client, relation, routine = coached(walk, funds=COACH_RATE * 2 + 5000)
    month_ends_at(relation, timezone.now() + timedelta(days=2, hours=20))
    run_the_coaching_hour()
    walk.ids(trainer=trainer.pk, relation=relation.pk, routine=routine.pk)


@state("coaching_stops_soon", area="coaching", role="client",
       summary="A paid coaching month ending in under 3 days that the client asked not "
               "to renew (stop_renewal on); the ending reminder has gone out.")
def coaching_stops_soon(walk):
    trainer, client, relation, routine = coached(walk, funds=COACH_RATE * 2 + 5000)
    call(walk, client, "post", "/api/v1/coaching/renewal/", {"renew": False})
    month_ends_at(relation, timezone.now() + timedelta(days=2, hours=20))
    run_the_coaching_hour()
    walk.ids(trainer=trainer.pk, relation=relation.pk, routine=routine.pk)


# ------------------------------------------------------------------ 3. plans
@state("plan_lapsed", area="coaching", role="client",
       summary="A plan paid from the wallet whose cover ended: frozen (the diet plans "
               "readable, nothing usable) and asked to re-subscribe.")
def plan_lapsed(walk):
    from subscription.tasks import expire_lapsed_subscriptions, send_renewal_reminders

    client, subscription = subscribed_client(walk, diet=True)
    plan_ends_at(subscription, timezone.now() - timedelta(days=1, hours=2))
    expire_lapsed_subscriptions()
    send_renewal_reminders()
    walk.ids(subscription=str(subscription.pk), plan=subscription.plan_id)


@state("plan_not_renewing", area="coaching", role="client",
       summary="An active plan the client turned off renewing: it runs to its end date.")
def plan_not_renewing(walk):
    client, subscription = subscribed_client(walk)
    call(walk, client, "post",
         f"/api/subscription/v1/subscriptions/{subscription.pk}/cancel/", {})
    walk.ids(subscription=str(subscription.pk), plan=subscription.plan_id)


@state("plan_renewal_due", area="coaching", role="client",
       summary="An active plan ending within 3 days: renewal_due, and the reminder sent.")
def plan_renewal_due(walk):
    from subscription.tasks import send_renewal_reminders

    client, subscription = subscribed_client(walk)
    plan_ends_at(subscription, timezone.now() + timedelta(days=2, hours=12))
    send_renewal_reminders()
    walk.ids(subscription=str(subscription.pk), plan=subscription.plan_id)


# ------------------------------------------------------------------ 6. before a coaching
@state("client_request_waiting", area="coaching", role="client",
       summary="A client whose request to a trainer is waiting for the trainer's answer "
               "(the quote shown; the fee is held only once the trainer accepts).")
def client_request_waiting(walk):
    trainer = coach(walk)
    client = funded_client(walk, amount=COACH_RATE + 5000)
    relation = ask_for(walk, client, trainer)
    walk.ids(trainer=trainer.pk, request=relation.pk)


@state("client_awaiting_first_program", area="coaching", role="client",
       summary="A client the trainer accepted: month 1 held, the first programme not "
               "sent yet.")
def client_awaiting_first_program(walk):
    trainer, client, relation, _routine = coached(walk, funds=COACH_RATE + 5000,
                                                  deliver=False)
    walk.ids(trainer=trainer.pk, relation=relation.pk)


@state("client_invited_by_coach", area="coaching", role="client",
       summary="A client a trainer invited, at the trainer's price; the wallet covers it.")
def client_invited_by_coach(walk):
    from users.models import TrainerClientRelation

    trainer = coach(walk)
    client = funded_client(walk, amount=COACH_RATE + 5000)
    call(walk, trainer, "post", "/api/auth/trainer/assign-client/",
         {"client_id": client.pk, "message": "أهلاً! أنا مدرّبك المقترح لبرنامج القوة."})
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    walk.ids(trainer=trainer.pk, request=relation.pk)


# ------------------------------------------------------------------ 7. a plan in reach
@state("client_wallet_covers_plan", area="coaching", role="client",
       summary="A client with no plan whose wallet covers the cheapest plan exactly.")
def client_wallet_covers_plan(walk):
    plan = cheapest_plan()
    client = walk.account(role="client")
    top_up(walk, agent(walk), client, plan_price(plan, client).amount)
    walk.ids(plan=plan.pk)
