"""Walk states: diet plans and their questions (`users/walk/__init__.py` says how to add
one).

Owned by the phase 3 worker.
"""
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
