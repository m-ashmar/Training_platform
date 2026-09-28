"""The plan catalogue a new database starts with, in both languages.

One definition for the two commands that write it — `setup_subscription_plans` (the
original) and `bootstrap_platform` (everything a new database needs) — so they cannot
drift apart.

**The prices are the ones `setup_subscription_plans` has always written; they are
placeholders until the owner sets the real ones in the console.** Nothing here ever
changes a plan that exists. A default plan is found by its `catalogue_key` (the `key`
below), which the console cannot edit, so a plan the operator renamed or repriced is
still the one found and left as the operator last saved it. A plan from before the key
(migration `0019`) is adopted by its ENGLISH name (`name_en`, because `name` is a
translated field and a lookup on it follows whatever language happens to be active —
the trap `setup_diet_templates` documents). The only writes to an existing row are its
key when it is adopted, and its Arabic name and description when those are still EMPTY:
the plan list is the last screen before a payment and the app is Arabic-first
(`subscription/translation.py`).

**The catalogue is written on a database that has NO plans, and only there** (SIDE_BUGS
S-236). `bootstrap_platform` runs again before each store submission, on production:
when it re-created every default it could not find, a plan the operator had renamed
came back as a second ACTIVE plan at the placeholder price, a plan the operator had
deleted came back on sale, and a rename of the English name alone died on the unique
Arabic one and stopped the bootstrap. Once any plan exists the catalogue is the
operator's: a missing default is reported (`"missing"`), never written. A failure of
one plan's write (the Arabic being filled in is already another plan's name) is
reported too, and the other plans still run.

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
        "key": "free", "name": "Free Plan", "name_ar": "الخطة المجانية",
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
        "key": "basic", "name": "Basic Plan", "name_ar": "الخطة الأساسية",
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
        "key": "premium", "name": "Premium Plan", "name_ar": "الخطة المميزة",
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
        "key": "professional", "name": "Professional Plan", "name_ar": "الخطة الاحترافية",
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

#: The plan the store-review client is granted (`seed_review_accounts`): found by its
#: key, whatever the operator has renamed it to. The name is for messages.
PREMIUM_PLAN_KEY = "premium"
PREMIUM_PLAN_NAME = "Premium Plan"


def ensure_default_plans() -> list[tuple[object, str]]:
    """Write the plans of `DEFAULT_PLANS` on a database that has none. On one that has,
    find each (by key, or adopt it by English name) and write nothing else.

    Returns `[(plan, outcome)]` in catalogue order. `outcome` is `"created"`, `"found"`,
    `"arabic added"` (found, and its empty Arabic filled in), `"missing"` (not found on
    a database that has plans, so NOT written: `plan` is an unsaved row carrying the
    default's names) or `"failed: <reason>"` (this plan's write failed and was rolled
    back, `plan` unsaved as for missing; the others still ran). Runs in the DEFAULT
    language so the translated columns land where they belong whatever locale the
    caller is in.
    """
    from django.conf import settings
    from django.db import IntegrityError, transaction
    from django.utils import translation

    from .models import SubscriptionPlan

    results = []
    with translation.override(settings.LANGUAGE_CODE), transaction.atomic():
        had_plans = SubscriptionPlan.objects.exists()
        for spec in DEFAULT_PLANS:
            spec = dict(spec)
            key = spec.pop("key")
            name_ar, description_ar = spec.pop("name_ar"), spec.pop("description_ar")
            try:
                with transaction.atomic():
                    results.append(_ensure_plan(key, spec, name_ar, description_ar,
                                                may_create=not had_plans))
            except IntegrityError as exc:
                reason = (str(exc).strip().splitlines() or ["integrity error"])[0]
                results.append((SubscriptionPlan(name_en=spec["name"], name_ar=name_ar),
                                f"failed: {reason}"))
    return results


def _ensure_plan(key, spec, name_ar, description_ar, *, may_create):
    from .models import SubscriptionPlan

    plan = SubscriptionPlan.objects.filter(catalogue_key=key).first()
    adopted = False
    if plan is None:
        plan = (SubscriptionPlan.objects.filter(catalogue_key__isnull=True,
                                                name_en=spec["name"])
                .order_by("id").first())
        adopted = plan is not None
    if plan is None:
        if not may_create:
            # The operator's catalogue: renamed before the key existed, or deleted.
            return SubscriptionPlan(name_en=spec["name"], name_ar=name_ar), "missing"
        plan = SubscriptionPlan(
            name_en=spec["name"], description_en=spec["description"],
            name_ar=name_ar, description_ar=description_ar, catalogue_key=key,
            # Free is written switched off: see the module docstring.
            is_active=spec["price"] > 0, **spec)
        plan.save()
        return plan, "created"
    filled = []
    if adopted:
        plan.catalogue_key = key
        filled.append("catalogue_key")
    arabic = {"name_ar": name_ar, "description_ar": description_ar}
    for field, text in arabic.items():
        if not (getattr(plan, field) or "").strip():
            setattr(plan, field, text)
            filled.append(field)
    if filled:
        plan.save(update_fields=[*filled, "updated_at"])
    return plan, "arabic added" if set(arabic) & set(filled) else "found"
