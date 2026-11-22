"""Road to 10 S0.6: home's cached page is dropped after the person's own action.

`GET /api/v1/home` is cached per person for 120 s, and its entry is retired by bumping the
person's stamp (`home/views.py cache_key`). A plan built, a request sent or answered, a
top-up credited, a programme bought and a subscription changed must each retire it, so
the first read after any of them is fresh. On the code before this change three of them
did not: a diet plan (created, or a trainer's draft sent with `.update()`), a ledger row
credited to the wallet, and a subscription saved. Requests already did and are asserted
here too; a programme bought is `tests/test_home_records.py
test_a_programme_just_bought_is_owned_on_the_card_at_once`.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"


def _stamp(user):
    from home.views import _stamp as stamp

    return stamp(user.pk)


@pytest.fixture
def fresh_cache():
    from training_platform.cache import private_cache

    private_cache().clear()
    yield
    private_cache().clear()


def test_a_diet_plan_built_retires_the_home(make_user, fresh_cache,
                                            django_capture_on_commit_callbacks):
    from diet.models import DietPlan

    client = make_user("s06_plan")
    before = _stamp(client)
    today = timezone.localdate()
    with django_capture_on_commit_callbacks(execute=True):
        DietPlan.objects.create(user=client, goal="Maintain", daily_calories=2000.0,
                                start_date=today, end_date=today + timedelta(days=6),
                                is_active=True)
    assert _stamp(client) != before


def test_a_trainers_draft_sent_retires_the_clients_home(make_user, fresh_cache,
                                                        django_capture_on_commit_callbacks):
    """Sending a draft makes it the client's plan with a queryset `.update()`, which no
    model signal sees (`diet/trainer_services.py _publish`)."""
    from diet.models import DietPlan
    from diet.trainer_services import TrainerDietPlanService

    trainer = make_user("s06_draft_t", user_type="trainer")
    client = make_user("s06_draft_c")
    today = timezone.localdate()
    draft = DietPlan.objects.create(user=client, created_by=trainer, goal="Maintain",
                                    daily_calories=2000.0, start_date=today,
                                    end_date=today + timedelta(days=6), is_active=False,
                                    is_draft=True)
    service = TrainerDietPlanService.__new__(TrainerDietPlanService)
    service.trainer = trainer
    before = _stamp(client)
    with django_capture_on_commit_callbacks(execute=True):
        assert service._publish(draft) is not None
    assert _stamp(client) != before


def test_a_request_sent_and_answered_retire_the_home(make_user, api, fresh_cache,
                                                     django_capture_on_commit_callbacks):
    from tests.test_coaching_months import _fund
    from users.models import TrainerClientRelation

    trainer = make_user("s06_req_t", user_type="trainer", first_name="Rami",
                        trainer_hourly_rate=Decimal("100.00"))
    client = make_user("s06_req_c")
    _fund(client, "1000.00")
    before = _stamp(client)
    with django_capture_on_commit_callbacks(execute=True):
        sent = api(client).post("/api/auth/client/request-trainer/",
                                {"trainer_id": trainer.pk}, content_type=JSON)
    assert sent.status_code == 200, sent.content
    after_sending = _stamp(client)
    assert after_sending != before

    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    with django_capture_on_commit_callbacks(execute=True):
        answered = api(trainer).post("/api/auth/trainer/respond-to-request/",
                                     {"request_id": relation.pk, "action": "approve"},
                                     content_type=JSON)
    assert answered.status_code == 200, answered.content
    assert _stamp(client) != after_sending


def test_a_top_up_credited_retires_the_home(make_user, fresh_cache,
                                            django_capture_on_commit_callbacks):
    from policy.money import MoneyType
    from tests.test_coaching_months import _fund
    from wallet.models import move_funds_atomic

    client = make_user("s06_topup")
    wallet = _fund(client, "0.00")
    before = _stamp(client)
    with django_capture_on_commit_callbacks(execute=True):
        move_funds_atomic(None, wallet, Decimal("500.00"), tx_type="topup",
                          money_type=MoneyType.WALLET_MOVE)
    assert _stamp(client) != before


def test_a_subscription_changed_retires_the_home(make_user, fresh_cache,
                                                 django_capture_on_commit_callbacks):
    from subscription.models import Subscription
    from tests.test_c9_payments import _plan

    client = make_user("s06_sub")
    before = _stamp(client)
    with django_capture_on_commit_callbacks(execute=True):
        sub = Subscription.objects.create(
            user=client, plan=_plan("s06 plan", has_diet_access=True), status="active",
            source="shamcash")
    created = _stamp(client)
    assert created != before
    with django_capture_on_commit_callbacks(execute=True):
        sub.status = "cancelled"
        sub.save()
    assert _stamp(client) != created
