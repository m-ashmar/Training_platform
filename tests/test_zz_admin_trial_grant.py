"""`POST /api/subscription/v1/admin/management/` under real concurrency: two operators
grant the same person a trial at the same moment.

The existence check and the create ran unlocked, so both passed the check and the second
create hit the one-subscription-per-person constraint: a 500. The grant now runs under
the person's row lock, so the second waits, sees the first's subscription and answers
400 `subscription_exists`. The gap is held open on purpose: each request pauses at its
create until the other has reached its own (or 2 s pass, which is what the lock makes
happen).

In a `test_zz_` file because `django_db(transaction=True)` truncates every table when it
finishes; nothing seeded by a migration is relied on.
"""
import threading
from decimal import Decimal

import pytest

JSON = "application/json"
URL = "/api/subscription/v1/admin/management/"


@pytest.mark.django_db(transaction=True)
def test_two_grants_at_once_give_one_subscription_and_no_500(make_user, api, monkeypatch):
    from django.db import connections

    from subscription.models import Subscription, SubscriptionPlan

    plan = SubscriptionPlan.objects.create(
        name="trial grant race", plan_type="premium", description="d",
        price=Decimal("90000.00"), currency="SYP", duration_days=30, is_active=True)
    person = make_user("tgz_person")
    operators = [make_user(f"tgz_op_{n}", user_type="admin") for n in range(2)]

    real_create = Subscription.objects.create
    arrived = threading.Barrier(2)

    def create(**kwargs):
        try:
            arrived.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return real_create(**kwargs)

    monkeypatch.setattr(Subscription.objects, "create", create)
    answers = []
    start = threading.Barrier(2)

    def grant(operator):
        try:
            client = api(operator)
            start.wait(timeout=30)
            resp = client.post(URL, {"user_id": person.pk, "plan_id": plan.pk},
                               content_type=JSON)
            body = resp.json() if resp.content else {}
            answers.append((resp.status_code, body.get("code")))
        finally:
            connections.close_all()

    threads = [threading.Thread(target=grant, args=(op,)) for op in operators]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(answers, key=str) == sorted([(201, None), (400, "subscription_exists")],
                                              key=str), answers
    assert Subscription.objects.filter(user=person).count() == 1
