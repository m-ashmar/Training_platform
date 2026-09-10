"""The one way a rule-based plan is generated and saved.

The synchronous view, the Celery task and the AI path's fallback each assembled this
sequence themselves, and they disagreed: the fallback read a stale `daily_calorie_target`
(default 2000) and skipped the feasibility check the view performs. One function, three
callers.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from django.core.exceptions import ValidationError as DjangoValidationError

logger = logging.getLogger(__name__)

#: What the deterministic planner can structure. More meals than this is a request for
#: the meal count the planner does not split for; it is clamped, not refused.
MAX_MAIN_MEALS = 3
#: Four, not one. This is a STRUCTURE cap — how many eating occasions a day may have —
#: not a nutrition rule, and at one snack the largest structure the planner would accept
#: topped out at 3,325 kcal on `maintain`. `CustomUser.MAX_DAILY_CALORIES` is 6,000 and
#: `calculate_daily_calories` legitimately returns more than 3,325 for a large or very
#: active adult, so four of ten plausible adult profiles were refused a plan outright —
#: and the suggestion handed back was 3 meals and 1 snack, which is the default and the
#: structure that had just been refused. There was no input that produced a plan for
#: them. Each snack adds `policy.snack_kcal` to the ceiling.
MAX_SNACKS = 4
#: Seven days inline is the ceiling for a synchronous endpoint; the task takes more.
MAX_INLINE_DAYS = 7
MAX_TASK_DAYS = 30


class InfeasibleCaloriesError(ValueError):
    """The requested energy cannot be served in this many meals and snacks."""

    def __init__(self, requested: float, ceiling: float, meal_count: int, snack_count: int):
        super().__init__(f"{requested:.0f} kcal cannot be served in {meal_count} meals and {snack_count} snacks")
        self.requested, self.ceiling = requested, ceiling
        self.meal_count, self.snack_count = meal_count, snack_count

    @property
    def suggestion(self) -> dict:
        """A structure that would actually work, or an honest admission that none does.

        This used to return the maximum structure unconditionally — which, when the
        request already WAS the maximum, told the client to retry with the numbers that
        had just been refused. A suggestion that cannot succeed is worse than none: it
        sends someone round a loop instead of telling them what is wrong.

        So: find the smallest structure that clears the requested figure, and if none
        does, say so and report the ceiling and the shortfall.
        """
        from diet.services.rule_based_planner import feasible_kcal_ceiling

        for snacks in range(self.snack_count, MAX_SNACKS + 1):
            for meals in range(max(self.meal_count, 1), MAX_MAIN_MEALS + 1):
                if feasible_kcal_ceiling(meals, snacks) >= self.requested:
                    return {"meal_count": meals, "snack_count": snacks,
                            "note": "This structure can carry that many calories."}

        best = feasible_kcal_ceiling(MAX_MAIN_MEALS, MAX_SNACKS)
        return {
            "meal_count": MAX_MAIN_MEALS,
            "snack_count": MAX_SNACKS,
            "max_daily_calories": round(best),
            "short_by": round(max(0.0, self.requested - best)),
            "note": "Even the largest structure cannot carry that many calories.",
        }


def generate_and_save(user, *, meal_count: int = 3, snack_count: int = 1, duration_days: int = 1,
                      start_date=None, no_repeat_days: int = 3, seed_salt: Optional[str] = None,
                      max_days: int = MAX_INLINE_DAYS) -> Tuple[object, object]:
    """Plan and persist. Returns (plan_output, diet_plan).

    Raises `ValueError` for an incomplete profile, `InfeasibleCaloriesError` when the
    meal structure cannot carry the energy, and `NoServableMealError` when the client's
    pool cannot supply a slot. Each is a message for the client, not a 500.
    """
    from diet.services.diet_persistence import DietPersistenceService
    from diet.services.rule_based_planner import RuleBasedPlanner, feasible_kcal_ceiling

    if getattr(user, "is_trainer", False):
        raise PermissionError("trainers do not generate client plans here")
    meal_count = max(1, min(MAX_MAIN_MEALS, int(meal_count)))
    snack_count = max(0, min(MAX_SNACKS, int(snack_count)))
    duration_days = max(1, min(max_days, int(duration_days)))

    kcal = user.calculate_daily_calories()   # ValueError if the profile is incomplete
    if not kcal or float(kcal) <= 0:
        raise ValueError("Complete profile required: height, weight, age, gender")
    daily_kcal = float(kcal)
    ceiling = feasible_kcal_ceiling(meal_count, snack_count, user.resolve_fitness_goal())
    # No slack. `feasible_kcal_ceiling` solves the constraint exactly — the largest meal
    # of the split must fit under `max_kcal_per_meal` — and multiplying by 1.05 reopened
    # a 166 kcal band the function exists to refuse. Measured at 3 meals + 1 snack on
    # `maintain`: the ceiling is 3,325 and 3,400 was accepted, giving a lunch of 1,280
    # against the same policy's 1,250 limit. The plan was then built short and stored
    # with the requested figure on it, which is the exact failure this gate is for.
    if daily_kcal > ceiling:
        raise InfeasibleCaloriesError(daily_kcal, ceiling, meal_count, snack_count)

    planner = RuleBasedPlanner(user, seed_salt=seed_salt)
    try:
        plan_output = planner.generate(
            daily_kcal=daily_kcal, meal_count=meal_count, snack_count=snack_count,
            start_date=start_date, duration_days=duration_days, no_repeat_days=no_repeat_days)
    except DjangoValidationError as exc:
        # `validate_diet_generation` refuses e.g. under 1,000 kcal/day. A legitimate
        # small profile used to reach the view's broad handler as a 500, forever.
        raise ValueError("; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)) from exc
    diet_plan = DietPersistenceService(user).save_plan(
        plan_output, meal_count=meal_count, snack_count=snack_count, start_date=start_date)
    return plan_output, diet_plan
