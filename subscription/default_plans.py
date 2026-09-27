"""The plan catalogue a new database starts with, in both languages.

One definition for the two commands that write it — `setup_subscription_plans` (the
original) and `bootstrap_platform` (everything a new database needs) — so they cannot
drift apart.

**The prices are the ones `setup_subscription_plans` has always written; they are
placeholders until the owner sets the real ones in the console.** Nothing here ever
changes a plan that exists: a plan is found by its ENGLISH name (`name_en`, because
`name` is a translated field and a lookup on it follows whatever language happens to be
active — the trap `setup_diet_templates` documents), and a plan that is found is left as
the operator last saved it. The only write to an existing row is its Arabic name and
description when those are still EMPTY: the plan list is the last screen before a
payment and the app is Arabic-first (`subscription/translation.py`).

**A plan priced 0 is written INACTIVE.** Subscribing to a free plan leaves the
subscription `pending` for ever: `POST v1/subscriptions/` creates it without a payment,
the Pay button answers `payment_not_required`, and nothing else activates it
(`subscription/views.py`). An active free plan on a new database is therefore a trap for
the first customers who pick it. The row exists so the operator can turn it on in the
console once a free plan is granted by something; until then it is not offered.
"""
from __future__ import annotations

from decimal import Decimal

DEFAULT_PLANS = (
    {
        "name": "Free Plan", "name_ar": "الخطة المجانية",
        "description": "Basic access with limited features. Perfect for trying out the "
                       "platform.",
        "description_ar": "وصول أساسي بميزات محدودة، مناسب لتجربة المنصة.",
        "plan_type": "basic", "price": Decimal("0.00"), "currency": "SYP",
        "duration_days": 30,
        "has_diet_access": True, "has_routine_access": False,
        "has_challenges_access": False, "has_ai_advice": False,
        "has_priority_support": False, "max_meals_per_day": 1, "max_routines": 1,
    },
    {
        "name": "Basic Plan", "name_ar": "الخطة الأساسية",
        "description": "Essential features for beginners. Includes diet planning and "
                       "basic routines.",
        "description_ar": "الميزات الأساسية للمبتدئين، وتشمل تخطيط النظام الغذائي وبرامج "
                          "تدريب أساسية.",
        "plan_type": "basic", "price": Decimal("1000.00"), "currency": "SYP",
        "duration_days": 30,
        "has_diet_access": True, "has_routine_access": True,
        "has_challenges_access": False, "has_ai_advice": False,
        "has_priority_support": False, "max_meals_per_day": 3, "max_routines": 3,
    },
    {
        "name": "Premium Plan", "name_ar": "الخطة المميزة",
        "description": "Advanced features with AI-powered diet advice and unlimited "
                       "access.",
        "description_ar": "ميزات متقدمة مع نصائح غذائية مدعومة بالذكاء الاصطناعي ووصول "
                          "غير محدود.",
        "plan_type": "premium", "price": Decimal("2500.00"), "currency": "SYP",
        "duration_days": 30,
        "has_diet_access": True, "has_routine_access": True,
        "has_challenges_access": True, "has_ai_advice": True,
        "has_priority_support": False, "max_meals_per_day": 5, "max_routines": 10,
    },
    {
        "name": "Professional Plan", "name_ar": "الخطة الاحترافية",
        "description": "Complete access with priority support and unlimited features.",
        "description_ar": "وصول كامل مع دعم ذي أولوية وميزات غير محدودة.",
        "plan_type": "pro", "price": Decimal("5000.00"), "currency": "SYP",
        "duration_days": 30,
        "has_diet_access": True, "has_routine_access": True,
        "has_challenges_access": True, "has_ai_advice": True,
        # 0 is "unlimited" for both limits (`subscription/quota.py`).
        "has_priority_support": True, "max_meals_per_day": 0, "max_routines": 0,
    },
)

#: The plan the store-review client is granted (`seed_review_accounts`), by English name.
PREMIUM_PLAN_NAME = "Premium Plan"


def ensure_default_plans() -> list[tuple[object, str]]:
    """Write every plan of `DEFAULT_PLANS` that does not exist yet.

    Returns `[(plan, outcome)]` in catalogue order, where `outcome` is `"created"`,
    `"found"`, or `"arabic added"` (found, and its empty Arabic filled in). Runs in the
    DEFAULT language so the translated columns land where they belong whatever locale
    the caller is in.
    """
    from django.conf import settings
    from django.db import transaction
    from django.utils import translation

    from .models import SubscriptionPlan

    results = []
    with translation.override(settings.LANGUAGE_CODE), transaction.atomic():
        for spec in DEFAULT_PLANS:
            spec = dict(spec)
            name_ar, description_ar = spec.pop("name_ar"), spec.pop("description_ar")
            plan = SubscriptionPlan.objects.filter(name_en=spec["name"]).order_by("id").first()
            if plan is None:
                plan = SubscriptionPlan(
                    name_en=spec["name"], description_en=spec["description"],
                    name_ar=name_ar, description_ar=description_ar,
                    # Free is written switched off: see the module docstring.
                    is_active=spec["price"] > 0, **spec)
                plan.save()
                results.append((plan, "created"))
                continue
            filled = []
            if not (plan.name_ar or "").strip():
                plan.name_ar = name_ar
                filled.append("name_ar")
            if not (plan.description_ar or "").strip():
                plan.description_ar = description_ar
                filled.append("description_ar")
            if filled:
                plan.save(update_fields=[*filled, "updated_at"])
            results.append((plan, "arabic added" if filled else "found"))
    return results
