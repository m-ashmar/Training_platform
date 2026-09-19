"""The platform plan after it ends (the owner, 2026-09-19): a reminder 3 days before, then
the last diet plan stays readable and nothing can be used, with a reminder on each step
of SUBSCRIPTION_LAPSED_REMINDER_DAYS. See subscription/models.py `is_frozen`,
subscription/permissions.py `HasDietAccessOrFrozenRead`, subscription/tasks.py.
"""
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from tests.test_c9_payments import _plan

pytestmark = pytest.mark.django_db

READS = ("/api/diet/v1/my/diet-plans/", "/api/diet/v1/client/progress/weekly/")
USES = (("get", "/api/diet/v1/advice/latest/"),
        ("get", "/api/diet/v1/food/list/"),
        ("post", "/api/diet/v1/plans/generate-rule/"),
        ("post", "/api/diet/v1/client/meals/interact/"),
        ("get", "/api/ai/sessions/"))


@pytest.fixture(autouse=True)
def owners_rules(settings):
    settings.SUBSCRIPTION_RENEWAL_NOTICE_DAYS = 3
    settings.SUBSCRIPTION_LAPSED_REMINDER_DAYS = (0, 3, 7, 14, 30)


def _subscriber(make_user, *, status="active", ends_in=timedelta(days=20), source="shamcash",
                diet=True, trial_ends_in=None):
    from subscription.models import Subscription

    tag = uuid.uuid4().hex[:8]
    user = make_user(f"sf_{tag}")
    plan = _plan(f"sf {tag}", has_diet_access=diet, has_ai_advice=True)
    sub = Subscription.objects.create(user=user, plan=plan, status="active", source=source)
    values = {"status": status, "end_date": timezone.now() + ends_in,
              "start_date": timezone.now() - timedelta(days=30)}
    if trial_ends_in is not None:
        values["trial_end_date"] = timezone.now() + trial_ends_in
    Subscription.objects.filter(pk=sub.pk).update(**values)
    return type(user).objects.get(pk=user.pk), Subscription.objects.get(pk=sub.pk)


def _call(api, user, method, path):
    client = api(user)
    if method == "post":
        return client.post(path, {}, content_type="application/json")
    return client.get(path)


@pytest.mark.parametrize("status", ["expired", "active"])   # active = before the sweep
def test_a_lapsed_subscriber_reads_their_plans_and_uses_nothing(make_user, api, status):
    user, sub = _subscriber(make_user, status=status, ends_in=-timedelta(hours=2))
    assert sub.is_frozen and sub.access == "frozen"

    for path in READS:
        assert api(user).get(path).status_code == 200, path
    for method, path in USES:
        resp = _call(api, user, method, path)
        assert (resp.status_code, resp.json()["code"]) == (403, "no_entitlement"), (path, resp.content)

    current = api(user).get("/api/subscription/v1/subscriptions/current/").json()
    assert (current["access"], current["renewal_due"], current["is_active"]) == ("frozen", False, False)
    checked = api(user).post("/api/subscription/v1/access/check/", {"features": ["diet"]},
                             content_type="application/json").json()
    assert (checked["has_access"], checked["frozen"]) == (False, True)


def test_a_checkout_never_paid_is_not_frozen(make_user, api):
    user, sub = _subscriber(make_user, status="pending", ends_in=timedelta(days=30), source="")
    assert not sub.is_frozen and sub.access == "inactive"
    for path in READS:
        resp = api(user).get(path)
        assert (resp.status_code, resp.json()["code"]) == (403, "no_entitlement"), path


def test_a_lapsed_plan_without_diet_gives_no_diet_reads(make_user, api):
    user, _sub = _subscriber(make_user, status="expired", ends_in=-timedelta(days=1), diet=False)
    assert api(user).get(READS[0]).status_code == 403


def test_the_app_is_told_when_the_plan_ends_within_three_days(make_user, api):
    user, _sub = _subscriber(make_user, ends_in=timedelta(days=2, hours=5))
    current = api(user).get("/api/subscription/v1/subscriptions/current/").json()
    assert (current["access"], current["renewal_due"]) == ("active", True)
    ios = api(user).get("/api/subscription/v1/subscriptions/current/",
                        HTTP_X_APP_CHANNEL="ios").json()
    assert "plan_price" not in ios and ios["renewal_due"] is True


# ------------------------------------------------------------------- the notices
def _notices(user, event_type):
    from notifications.models import Notification

    return list(Notification.objects.filter(recipient=user, event_type=event_type))


def test_the_ends_soon_notice_goes_once_per_cover(make_user):
    from subscription.tasks import send_renewal_reminders

    user, sub = _subscriber(make_user, ends_in=timedelta(days=2, hours=12))
    early, _ = _subscriber(make_user, ends_in=timedelta(days=5))
    pending, _ = _subscriber(make_user, status="pending", ends_in=timedelta(days=2, hours=12),
                             source="")

    send_renewal_reminders()
    send_renewal_reminders()
    assert len(_notices(user, "subscription_expiring")) == 1
    assert not _notices(early, "subscription_expiring")
    assert not _notices(pending, "subscription_expiring")
    (notice,) = _notices(user, "subscription_expiring")
    assert notice.metadata["context"]["days"] == 3

    # Renewed: the end moved, so the next cover is reminded again.
    from subscription.models import Subscription
    Subscription.objects.filter(pk=sub.pk).update(end_date=sub.end_date + timedelta(days=30))
    send_renewal_reminders(now=timezone.now() + timedelta(days=30))
    assert len(_notices(user, "subscription_expiring")) == 2


def test_a_trial_is_reminded_by_its_own_end(make_user):
    from subscription.tasks import send_renewal_reminders

    user, _ = _subscriber(make_user, status="trial", ends_in=timedelta(days=30),
                          trial_ends_in=timedelta(days=2, hours=6), source="trial")
    send_renewal_reminders()
    assert len(_notices(user, "subscription_expiring")) == 1


def test_after_the_end_the_ask_repeats_on_its_steps_only(make_user):
    from subscription.tasks import send_renewal_reminders

    user, sub = _subscriber(make_user, status="expired", ends_in=-timedelta(minutes=5))
    end = sub.effective_end
    for day in range(0, 35):
        send_renewal_reminders(now=end + timedelta(days=day, hours=1))
    steps = sorted(int(n.related_object_id.rsplit(":", 1)[1])
                   for n in _notices(user, "subscription_expired"))
    assert steps == [0, 3, 7, 14, 30]


def test_the_notices_carry_no_price_and_no_link(make_user):
    from django.utils import translation

    from notifications.channels.fcm import _resolve_event_template

    for event_type, context in (("subscription_expiring", {"days": 3}),
                                ("subscription_expired", {}),
                                ("coaching_renewal_soon", {"name": "Rami", "days": 3}),
                                ("coaching_payment_due", {"name": "Rami", "days": 0})):
        template = _resolve_event_template(event_type)
        for language in ("en", "ar"):
            with translation.override(language):
                text = f"{template.title} {template.body % context}"
            assert "http" not in text and "SYP" not in text and "5000" not in text, text
