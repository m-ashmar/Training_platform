"""Walk states: diet plans and their questions (`users/walk/__init__.py` says how to add
one).

Owned by the phase 3 worker.
"""
from datetime import timedelta

from django.utils import timezone

from . import state
from .framework import agent_cash_gaps, needs

#: The client is handed this many times the plan's price in cash, at the state's one
#: agent: the plan, with something left over. Read by the state and by its `@needs`.
TIMES_THE_PRICE = 2


def _the_plan(currency):
    """The plan the state buys in `currency` (a new client's), or None."""
    from subscription.default_plans import PREMIUM_PLAN_KEY

    from .shared import choose_plan

    return choose_plan(currency, diet=True, prefer=PREMIUM_PLAN_KEY)


@needs("premium_no_diet_plan")
def _a_diet_plan_a_new_wallet_can_pay():
    """The state pays a plan with the diet from a new client's wallet: one must be on
    sale priced in the currency that wallet takes (never priced across currencies), and
    the agent must take TIMES_THE_PRICE its price in a day."""
    from .shared import missing_plan, new_wallet_currency

    plan = _the_plan(new_wallet_currency())
    if plan is None:
        return missing_plan(diet=True)
    return agent_cash_gaps("premium_no_diet_plan", in_a_day=plan.price * TIMES_THE_PRICE)


@state("premium_no_diet_plan", area="diet", role="client",
       summary="A Premium client, the plan paid from their wallet after an agent's cash "
               "top-up, onboarded, who has never answered the allergy question nor built "
               "a diet plan (the allergy page comes before the first plan, F1.8).")
def premium_no_diet_plan(walk):
    import uuid

    from subscription.default_plans import PREMIUM_PLAN_KEY

    from .shared import open_agent, plan_for, top_up

    me = walk.account()
    agent = open_agent(walk)
    # A plan with the diet priced in the client's OWN wallet currency (the catalogue's
    # Premium when it is one): no price is converted and no rate is needed or set.
    plan = plan_for(me, diet=True, prefer=PREMIUM_PLAN_KEY)
    # Enough for the plan, with something left over, in cash at the agent.
    top_up(walk, agent, me, f"{plan.price * TIMES_THE_PRICE:.2f}", plan.currency)

    # The checkout the app opens, paid from the wallet (Road to 10 S2.13): the plan
    # starts now, the payment completed with its receipt, 100% to the platform.
    _s, subscription = walk.api(me, "post", "/api/subscription/v1/subscriptions/",
                                {"plan_id": plan.pk})
    _s, paid = walk.api(
        me, "post", f"/api/subscription/v1/subscriptions/{subscription['id']}/pay-from-wallet/",
        {"idempotency_key": f"walk-{uuid.uuid4().hex[:24]}",
         "amount": f"{plan.price:.2f}", "currency": plan.currency})
    # No food preferences row: the allergies were never answered. No diet plan.
    walk.ids(plan=plan.pk, subscription=subscription["id"],
             payment=paid.get("payment_id"), agent=agent.pk)


# ------------------------------------------------------------------ a lapsed plan's diet
#: `plan_lapsed_with_diet`'s client is handed exactly the plan's price, as
#: `plan_lapsed`'s is (`users/walk/coaching.py` CASH): one plan, nothing left over.
LAPSED_DIET_DAYS = 7


@needs("plan_lapsed_with_diet")
def _a_lapsed_plan_with_a_diet_to_read():
    """The state buys a plan with the diet from a new client's wallet (one on sale in the
    currency a new wallet takes; an agent who takes its price in a day), and builds the
    client's diet plan with the platform's planner, which needs a food catalogue it may
    plan from (vetted foods: `manage.py bootstrap_platform` writes it)."""
    from diet.models import FoodItem

    from .shared import choose_plan, missing_plan, new_wallet_currency

    plan = choose_plan(new_wallet_currency(), diet=True)
    gaps = (missing_plan(diet=True) if plan is None
            else agent_cash_gaps("plan_lapsed_with_diet", in_a_day=plan.price))
    if not FoodItem.objects.filter(needs_review=False).exists():
        gaps.append("a food catalogue the diet planner may plan from (vetted foods; "
                    "`manage.py bootstrap_platform` writes it)")
    return gaps


@state("plan_lapsed_with_diet", area="diet", role="client",
       summary="plan_lapsed, with a 7-day diet plan the client built (the platform's "
               "planner) BEFORE the cover ended, today's meals in it: frozen, the plan "
               "readable, nothing usable (the owner's freeze rule).")
def plan_lapsed_with_diet(walk):
    """The same lapse as `plan_lapsed` (`users/walk/coaching.py`, its helpers, not its
    state, which stays as it is): the cheapest plan with the diet in the client's own
    currency, paid from a wallet an agent topped up with its price exactly; then, while
    it is active, the client builds a week's diet plan through the app's route
    (`POST /api/diet/v1/plans/generate-rule/`: the rule engine, no model), starting
    today; then the cover ends a day ago, the daily run lapses it and sends its
    reminders. What the client reads then: `GET v1/client/progress/` with
    `has_active_plan: true` and today's meals, `GET v1/my/diet-plans/` the plan, and
    every use (ticking a meal, generating) answered 403 `no_entitlement`."""
    from .coaching import buy_plan, lapse_now, plan_ends_at, plan_price, plan_reminders_now
    from .shared import plan_for, top_up
    from .wallet import agent

    client = walk.account(role="client")
    plan = plan_for(client, diet=True)
    top_up(walk, agent(walk), client, plan_price(plan, client).amount)
    subscription = buy_plan(walk, client, plan)

    # A fresh load, as a real request makes: this object still holds the subscription
    # it read before the payment (pending), and the diet routes read it off the user.
    client = type(client).objects.get(pk=client.pk)
    _status, built = walk.api(client, "post", "/api/diet/v1/plans/generate-rule/",
                              {"duration_days": LAPSED_DIET_DAYS}, expect=(201,))
    diet_plan = built.get("id") or built.get("diet_plan_id")

    plan_ends_at(subscription, timezone.now() - timedelta(days=1, hours=2))
    lapse_now(subscription)
    plan_reminders_now(subscription)
    walk.ids(subscription=str(subscription.pk), plan=subscription.plan_id,
             diet_plan=diet_plan)
