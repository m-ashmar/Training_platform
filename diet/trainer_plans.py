"""A trainer's nutrition plan, read back: its meals parsed, its row, its review.

BACKEND_BUGS B-131..B-134, from the design's `14-trainer-nutrition` screens
(`TrainerPlanForm`, `TrainerPlanReview`, `TrainerClientNutrition`).

- `parse_meals` — a plan's meals from a request body, the shape `trainer/meals/` takes
  minus `diet_plan_id`, each refusal naming its `index`.
- `plan_row` / `adherence_by_plan` — what a row of `GET trainer/diet-plans/` says beyond
  the flag `is_active`: why it is or is not followed (`DietPlan.status_on`) and how well
  it was followed.
- `coaching_refusal` — the client's coaching state, when it is one a trainer may not see
  the client's data in.
- `review` — per day, the plan against its target with the ENGINE's tolerance, the
  averages, and the allergen / dislike verdict against the client's own preferences.

Nothing here is engine logic: the arithmetic and the tolerance are read from
`diet/planner` (`report.totals_of`, `report.deviation_of`, `MacroDeviation.within`,
`policy.load_policy(...).tolerance`), never restated.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Dict, Iterable, List, Optional

from django.core.exceptions import ValidationError
from django.utils.dateparse import parse_date, parse_time
from django.utils.translation import gettext as _

from .trainer_services import MAX_PLAN_MEALS, MealRefused

#: The nutrients a review reports, as the API names them and as the engine does.
NUTRIENTS = (("calories", "calories"), ("protein", "protein"), ("carbs", "carb"),
             ("fat", "fat"))


# ------------------------------------------------------------------------ parsing
def parse_meals(raw) -> List[dict]:
    """The meals of a plan sent whole (or reviewed before it is sent).

    Each item is `{meal_type, target_date (YYYY-MM-DD), food_items: [{food_id,
    quantity}], scheduled_time? (HH:MM), description?}` — one `trainer/meals/` item
    without `diet_plan_id`. Refusals: `bad_meals` (not a non-empty list),
    `batch_too_large` (more than `MAX_PLAN_MEALS`, with `limit`), and per item, with its
    `index`: `bad_meals` (the item is not an object), `missing_fields` (with `fields`),
    `bad_date`, `bad_time`. What the foods and slots are worth is the service's to judge
    (`TrainerDietPlanService.prepare_meals`).
    """
    if not isinstance(raw, list) or not raw:
        raise ValidationError(_("meals must be a list of meals"), code="bad_meals")
    if len(raw) > MAX_PLAN_MEALS:
        refusal = ValidationError(
            _("At most %(n)d meals in one request") % {"n": MAX_PLAN_MEALS},
            code="batch_too_large")
        raise MealRefused(refusal, None, limit=MAX_PLAN_MEALS)
    meals = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise MealRefused(ValidationError(_("meals must be a list of meals"),
                                              code="bad_meals"), index)
        missing = [name for name in ("meal_type", "target_date", "food_items")
                   if not item.get(name)]
        if missing:
            raise MealRefused(ValidationError(_("Missing required fields"),
                                              code="missing_fields"), index, fields=missing)
        target_date = parse_date(item["target_date"]) if isinstance(
            item["target_date"], str) else None
        if target_date is None:
            raise MealRefused(ValidationError(_("Invalid target_date format"),
                                              code="bad_date"), index)
        scheduled_time = None
        if item.get("scheduled_time"):
            scheduled_time = (parse_time(item["scheduled_time"])
                              if isinstance(item["scheduled_time"], str) else None)
            if scheduled_time is None:
                raise MealRefused(ValidationError(
                    _("Invalid scheduled_time format. Use HH:MM."), code="bad_time"), index)
        description = item.get("description") or ""
        meals.append({"meal_type": item["meal_type"], "target_date": target_date,
                      "food_items": item["food_items"], "scheduled_time": scheduled_time,
                      "description": description if isinstance(description, str) else ""})
    return meals


# ------------------------------------------------------------------ coaching gate
def coaching_refusal(viewer, client) -> Optional[str]:
    """`coaching_frozen` / `coaching_unpaid` / `coaching_ended` while the coaching
    between trainer `viewer` and `client` is in a state that may not be used, else None.

    The rule `training_refusal` applies to the client's training and the chat applies to
    both sides (`users.coaching.refusals_for`, the same two queries): a coaching that
    was never paid for (free, or from before months existed) is not affected. Only a
    trainer is asked; for anyone else there is no pair to judge.
    """
    if not getattr(viewer, "is_trainer", False):
        return None
    from users.coaching import refusals_for

    return refusals_for(viewer.pk, [client.pk]).get(client.pk)


# --------------------------------------------------------------------- the rows
def adherence_by_plan(client, plans, today) -> Dict[int, Optional[float]]:
    """`{plan_id: percent}` for the plans that have one: the platform's adherence
    figure, `ai_assistant.tools.diet_tools.plan_day_adherence` over the plan's days —
    the number the client's AI coach quotes them.

    Computed from the TICKED meals: a `DailyProgress` row exists only for a day on which
    the client ticked something, a day scores 100 minus how far its calories landed from
    the plan's, and every day of the plan that held meals counts, untracked ones as 0.
    A plan with nothing ticked has no figure (no data is not a score), nor does a draft
    or a plan not yet started. One query finds the plans with a ticked day; the figure
    is then computed for those only.
    """
    from ai_assistant.tools.diet_tools import plan_day_adherence

    from .models import DailyProgress

    started = [p for p in plans if not p.is_draft and p.start_date <= today]
    if not started:
        return {}
    tracked = set(DailyProgress.objects
                  .filter(user=client, diet_plan_id__in=[p.id for p in started],
                          target_calories__gt=0, date__lte=today)
                  .order_by().values_list("diet_plan_id", flat=True).distinct())
    return {p.id: plan_day_adherence(client, p, p.start_date, p.end_date)["average"]
            for p in started if p.id in tracked}


def plan_row(plan, today, adherence=None) -> dict:
    """The fields B-133 and B-134 add to a trainer's plan row."""
    return {
        "status": plan.status_on(today),
        "duration_days": plan.duration_days,
        "duration_weeks": plan.duration_weeks,
        "adherence_percent": adherence,
    }


# -------------------------------------------------------------------- the review
def _nutrients(totals: dict) -> dict:
    return {api: round(float(totals.get(engine, 0.0)), 1) for api, engine in NUTRIENTS}


def _deviation(totals: dict, targets: dict, tolerance: dict):
    """Each nutrient's miss (`amount` in kcal or grams, `percent` of the target) and
    whether it is inside the engine's band for it, judged by the engine's own test."""
    from diet.planner.report import MacroDeviation, deviation_of

    deviation = deviation_of(totals, targets)
    out = {}
    for api, engine in NUTRIENTS:
        relative = getattr(deviation, engine)
        amount = deviation.absolute.get(engine, 0.0)
        alone = MacroDeviation(absolute={engine: amount}, **{engine: relative})
        out[api] = {"amount": round(amount, 1), "percent": round(relative * 100, 1),
                    "within": alone.within(tolerance)}
    return out, deviation.within(tolerance)


def _food_ref(food, dates) -> dict:
    from diet.services.meal_payload import name_en_of

    return {"food_id": food.id, "name": name_en_of(food),
            "name_ar": getattr(food, "name_ar", None) or name_en_of(food),
            "dates": [str(d) for d in sorted(dates)]}


def food_check(client, meals) -> dict:
    """The allergen and dislike verdict for every food of the plan, against the client's
    OWN preferences (`UserFoodPreference`): what `TrainerPlanReview` shows as «فحصنا كل
    المكوّنات». Health data: the callers show it only to the client's approved trainer.

    `allergens.verdict`: `nothing_declared`, `clear` (every food positively cleared),
    `unverified` (no declared allergen found, but a food's allergen data is not
    verified — not the same as safe, `AllergenReport.is_safe`), or `violation`.
    `dislikes.verdict`: `clear` or `present` (a food on the client's «لا يحب» list).
    """
    from .models import UserFoodPreference
    from .services.meal_validator import UNVERIFIED, VIOLATION, AllergenChecker

    preference = UserFoodPreference.objects.filter(user=client).first()
    checker = AllergenChecker(preference.allergies if preference else "")
    disliked = (set(preference.disliked_foods.values_list("id", flat=True))
                if preference else set())

    dates_of: Dict[int, set] = {}
    foods = {}
    for day, _meal_type, components in meals:
        for food, _grams in components:
            foods[food.id] = food
            dates_of.setdefault(food.id, set()).add(day)
    ordered = sorted(foods.values(), key=lambda f: (min(dates_of[f.id]), f.id))

    violations, unverified, dislikes = [], [], []
    for food in ordered:
        verdict = checker.check_food(food)
        if verdict.verdict == VIOLATION:
            violations.append({**_food_ref(food, dates_of[food.id]),
                               "allergens": sorted(verdict.matched)})
        elif verdict.verdict == UNVERIFIED:
            unverified.append(_food_ref(food, dates_of[food.id]))
        if food.id in disliked:
            dislikes.append(_food_ref(food, dates_of[food.id]))
    if not checker.active:
        allergen_verdict = "nothing_declared"
    else:
        allergen_verdict = ("violation" if violations else
                            "unverified" if unverified else "clear")
    return {
        "preferences_on_file": preference is not None,
        "declared_allergens": sorted(checker.user_tags),
        "allergens": {"verdict": allergen_verdict, "violations": violations,
                      "unverified": unverified},
        "dislikes": {"verdict": "present" if dislikes else "clear", "foods": dislikes},
    }


def review(*, client, goal, target: dict, start_date, end_date,
           meals: Iterable, plan=None, today=None) -> dict:
    """The plan, day by day, against its target — the numbers `TrainerPlanReview` draws.

    `meals` is `[(date, meal_type, [(FoodItem, grams)])]`; `target` is `{calories,
    protein, carbs, fat}` (the plan's own: the calories the trainer set and the macro
    grams they imply). A day's totals are `planner.report.totals_of` of its foods, the
    engine's one implementation; a nutrient is `within` its band exactly when the engine
    would call it so (`MacroDeviation.within`, which also forgives a miss too small to
    matter), and the bands are `load_policy(goal).tolerance` — 10 % on calories.

    Every day of the plan is a row, one with no meals included (`meals_count: 0`,
    never within tolerance: the client would have nothing to eat). The averages are over
    the days that hold meals.
    """
    from diet.planner.policy import load_policy
    from diet.planner.report import totals_of

    tolerance = load_policy(goal).tolerance
    targets = {engine: float(target[api] or 0.0) for api, engine in NUTRIENTS}
    meals = list(meals)
    by_day: Dict[object, list] = {}
    for day, meal_type, components in meals:
        by_day.setdefault(day, []).append(components)

    days, filled = [], []
    span = (end_date - start_date).days + 1
    for offset in range(span):
        day = start_date + timedelta(days=offset)
        plates = by_day.get(day, [])
        totals = totals_of([item for plate in plates for item in plate])
        deviation, within = _deviation(totals, targets, tolerance)
        if plates:
            filled.append(totals)
        days.append({"date": str(day), "day": offset + 1, "meals_count": len(plates),
                     "totals": _nutrients(totals), "deviation": deviation,
                     "within_tolerance": bool(plates) and within})

    averages = None
    if filled:
        mean = {engine: sum(t[engine] for t in filled) / len(filled)
                for _api, engine in NUTRIENTS}
        deviation, within = _deviation(mean, targets, tolerance)
        averages = {"totals": _nutrients(mean), "deviation": deviation,
                    "within_tolerance": within, "days_counted": len(filled)}

    with_meals = [d for d in days if d["meals_count"]]
    return {
        "plan": {
            "id": plan.id if plan is not None else None,
            "status": plan.status_on(today) if plan is not None else None,
            "client_id": client.id, "goal": goal,
            "start_date": str(start_date), "end_date": str(end_date),
            "duration_days": span,
        },
        "target": {api: round(float(target[api] or 0.0), 1) for api, _engine in NUTRIENTS},
        "tolerance_percent": {api: round(float(tolerance.get(engine, 0.10)) * 100, 1)
                              for api, engine in NUTRIENTS},
        "days": days,
        "averages": averages,
        "summary": {
            "days": len(days),
            "days_with_meals": len(with_meals),
            "empty_days": len(days) - len(with_meals),
            "days_within_tolerance": sum(1 for d in with_meals if d["within_tolerance"]),
            "days_outside_tolerance": sum(1 for d in with_meals if not d["within_tolerance"]),
        },
        "check": food_check(client, meals),
    }


def saved_plan_meals(plan) -> list:
    """`[(date, meal_type, [(FoodItem, grams)])]` of a saved plan, in three queries."""
    from django.db.models import Prefetch

    from .models import Meal, MealComponent

    rows = (Meal.objects.filter(diet_plan=plan)
            .prefetch_related(Prefetch("components",
                                       queryset=MealComponent.objects.select_related("food")
                                       .order_by("id")))
            .order_by("date", "scheduled_time", "id"))
    return [(m.date, m.meal_type, [(c.food, c.quantity) for c in m.components.all()])
            for m in rows]
