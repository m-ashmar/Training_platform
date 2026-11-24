"""Walk states: diet plans and their questions (`users/walk/__init__.py` says how to add
one).

Owned by the phase 3 worker.
"""
from . import state


@state("premium_no_diet_plan", area="diet", role="client",
       summary="A Premium client, the plan paid from their wallet after an agent's cash "
               "top-up, onboarded, who has never answered the allergy question nor built "
               "a diet plan (the allergy page comes before the first plan, F1.8).")
def premium_no_diet_plan(walk):
    import uuid

    from subscription.default_plans import PREMIUM_PLAN_KEY
    from subscription.models import SubscriptionPlan
    from subscription.pricing import charge_for
    from wallet import currency_for

    from .shared import open_agent, top_up

    me = walk.account()
    agent = open_agent(walk)
    plan = SubscriptionPlan.objects.get(catalogue_key=PREMIUM_PLAN_KEY, is_active=True)
    price = charge_for(plan.price, plan.currency, currency_for(me))
    # Enough for the plan, with something left over, in cash at the agent.
    top_up(walk, agent, me, f"{price.amount * 2:.2f}", price.currency)

    # The checkout the app opens, paid from the wallet (Road to 10 S2.13): the plan
    # starts now, the payment completed with its receipt, 100% to the platform.
    _s, subscription = walk.api(me, "post", "/api/subscription/v1/subscriptions/",
                                {"plan_id": plan.pk})
    _s, paid = walk.api(
        me, "post", f"/api/subscription/v1/subscriptions/{subscription['id']}/pay-from-wallet/",
        {"idempotency_key": f"walk-{uuid.uuid4().hex[:24]}",
         "amount": f"{price.amount:.2f}", "currency": price.currency})
    # No food preferences row: the allergies were never answered. No diet plan.
    walk.ids(plan=plan.pk, subscription=subscription["id"],
             payment=paid.get("payment_id"), agent=agent.pk)
