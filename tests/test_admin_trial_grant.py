"""`POST /api/subscription/v1/admin/management/`: an operator's free trial of a plan,
hardened (the manager's ruling of 2026-11-23, after the read-only pass over the routes
the app never calls).

It granted any plan, retired ones included, for any number of days (36500 was a
hundred-year Premium), to anyone with `is_staff`, wrote `source='trial'` as if the
person had started a trial themselves, left no record of who did it, and answered 500
for `trial_days: "abc"` and for two grants at once. Now, as the console's own grant:

- only someone the console would let grant: `is_staff` AND the
  `subscription.change_subscription` permission (a superuser holds every one); anyone
  else 403 `admins_only`;
- `source='operator_grant'`, `status` still `trial`;
- one admin log entry naming the operator;
- an inactive plan 400 `invalid_user_or_plan`;
- `trial_days` a whole number from 1 to the plan's `duration_days` (default 7, or the
  plan's length when shorter), else 400 `invalid_trial_days`, never a 500;
- under the person's row lock, so two grants at once give one subscription and one
  400 `subscription_exists` (tests/test_zz_admin_trial_grant.py).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"
URL = "/api/subscription/v1/admin/management/"


def _plan(tag, *, days=30, active=True):
    from subscription.models import SubscriptionPlan

    return SubscriptionPlan.objects.create(
        name=f"trial grant {tag}", plan_type="premium", description="d",
        price=Decimal("90000.00"), currency="SYP", duration_days=days, is_active=active)


def _grant(api, who, body):
    return api(who).post(URL, body, content_type=JSON)


def _code(resp):
    return resp.status_code, resp.json().get("code")


def test_the_grant_is_the_operators_and_is_logged(make_user, api):
    from django.contrib.admin.models import LogEntry
    from django.contrib.contenttypes.models import ContentType

    from subscription.models import Payment, Subscription

    operator = make_user("tg_operator", user_type="admin")
    person = make_user("tg_person")
    plan = _plan("logged")
    resp = _grant(api, operator, {"user_id": person.pk, "plan_id": plan.pk, "trial_days": 10})
    assert resp.status_code == 201, resp.content

    sub = Subscription.objects.get(user=person)
    assert (sub.status, sub.source) == ("trial", "operator_grant")
    assert abs((sub.trial_end_date - timezone.now()) - timedelta(days=10)) < timedelta(minutes=1)
    assert not Payment.objects.filter(subscription=sub).exists()
    entry = LogEntry.objects.get(
        content_type=ContentType.objects.get_for_model(Subscription), object_id=str(sub.pk))
    assert entry.user_id == operator.pk
    assert "10" in entry.change_message


def test_only_someone_the_console_lets_grant_may_grant(make_user, api):
    from django.contrib.auth.models import Permission

    from subscription.models import Subscription

    plan = _plan("who")
    bare_staff = make_user("tg_bare_staff", is_staff=True)
    resp = _grant(api, bare_staff, {"user_id": make_user("tg_p1").pk, "plan_id": plan.pk})
    assert _code(resp) == (403, "admins_only"), resp.content
    assert not Subscription.objects.filter(user__username="tg_p1").exists()

    for role in ("client", "trainer", "agent"):
        who = make_user(f"tg_{role}", user_type=role)
        assert _code(_grant(api, who, {"user_id": who.pk, "plan_id": plan.pk})) == \
            (403, "admins_only")

    allowed = make_user("tg_allowed_staff", is_staff=True)
    allowed.user_permissions.add(Permission.objects.get(
        codename="change_subscription", content_type__app_label="subscription"))
    resp = _grant(api, allowed, {"user_id": make_user("tg_p2").pk, "plan_id": plan.pk})
    assert resp.status_code == 201, resp.content


def test_an_inactive_plan_is_refused(make_user, api):
    from subscription.models import Subscription

    operator = make_user("tg_op_inactive", user_type="admin")
    person = make_user("tg_inactive")
    retired = _plan("retired", active=False)
    resp = _grant(api, operator, {"user_id": person.pk, "plan_id": retired.pk})
    assert _code(resp) == (400, "invalid_user_or_plan"), resp.content
    assert not Subscription.objects.filter(user=person).exists()


@pytest.mark.parametrize("days", [0, -3, 31, 36500, "abc", "", 1.5, "1.5", True, [], {}, None])
def test_trial_days_is_a_whole_number_up_to_the_plans_length(make_user, api, days):
    from subscription.models import Subscription

    operator = make_user("tg_op_days", user_type="admin")
    person = make_user("tg_days")
    resp = _grant(api, operator, {"user_id": person.pk, "plan_id": _plan("days").pk,
                                  "trial_days": days})
    assert _code(resp) == (400, "invalid_trial_days"), (days, resp.content)
    assert not Subscription.objects.filter(user=person).exists()


@pytest.mark.parametrize("days,expected", [(1, 1), (30, 30), ("12", 12), (None, 7)])
def test_a_whole_number_in_range_is_granted(make_user, api, days, expected):
    from subscription.models import Subscription

    operator = make_user("tg_op_ok", user_type="admin")
    person = make_user("tg_ok")
    body = {"user_id": person.pk, "plan_id": _plan("ok").pk}
    if days is not None:
        body["trial_days"] = days
    resp = _grant(api, operator, body)
    assert resp.status_code == 201, resp.content
    sub = Subscription.objects.get(user=person)
    assert abs((sub.trial_end_date - timezone.now()) - timedelta(days=expected)) < \
        timedelta(minutes=1)


def test_a_plan_shorter_than_a_week_defaults_to_its_own_length(make_user, api):
    from subscription.models import Subscription

    operator = make_user("tg_op_short", user_type="admin")
    person = make_user("tg_short")
    resp = _grant(api, operator, {"user_id": person.pk, "plan_id": _plan("short", days=3).pk})
    assert resp.status_code == 201, resp.content
    sub = Subscription.objects.get(user=person)
    assert abs((sub.trial_end_date - timezone.now()) - timedelta(days=3)) < timedelta(minutes=1)


@pytest.mark.parametrize("field,value", [("user_id", "abc"), ("plan_id", "abc"),
                                         ("user_id", [1]), ("plan_id", {"id": 1})])
def test_ids_that_are_not_ids_are_a_400(make_user, api, field, value):
    operator = make_user("tg_op_ids", user_type="admin")
    body = {"user_id": make_user("tg_ids").pk, "plan_id": _plan("ids").pk, field: value}
    assert _code(_grant(api, operator, body)) == (400, "invalid_user_or_plan")
