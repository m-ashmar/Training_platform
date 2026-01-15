from __future__ import annotations

from typing import Optional

from ..models import DietPlan, Meal
from ..utils.logging_utils import get_logger


class SnackCalorieEnforcer:
    """
    Force each snack meal to be ~200 kcal by proportionally scaling all its components.
    This runs post-persistence to guard against mapping changes and data anomalies.
    """

    def __init__(self, target_kcal: float = 200.0) -> None:
        self.target_kcal = float(target_kcal)
        self.logger = get_logger(__name__)

    def enforce(self, diet_plan: DietPlan, target_kcal: Optional[float] = None) -> None:
        desired = float(target_kcal) if target_kcal is not None else self.target_kcal
        if desired <= 0.0:
            return
        snacks = Meal.objects.filter(diet_plan=diet_plan, meal_type='Snack').prefetch_related('components', 'components__food')
        for snack in snacks:
            # Compute calories robustly from per-gram values with fallback
            cur = 0.0
            for comp in snack.components.all():
                food = comp.food
                kcal_pg = float(getattr(food, 'calories_per_gram', 0.0) or 0.0)
                if kcal_pg <= 0.0:
                    cals_per_serv = float(getattr(food, 'calories', 0.0) or 0.0)
                    serv_g = float(getattr(food, 'serving_size_grams', 100.0) or 100.0)
                    kcal_pg = (cals_per_serv / serv_g) if serv_g > 0 else 0.0
                cur += kcal_pg * float(comp.quantity or 0.0)
            if cur <= 0.0:
                continue
            scale = desired / cur
            # Bound extreme scale just in case
            if scale <= 0.0:
                continue
            for comp in snack.components.all():
                comp.quantity = float(comp.quantity or 0.0) * scale
                comp.save(update_fields=['quantity'])
            try:
                self.logger.info(
                    'snack_kcal_enforced',
                    extra={
                        'diet_plan_id': diet_plan.id,
                        'meal_id': snack.id,
                        'before_kcal': round(cur, 1),
                        'after_kcal': desired,
                        'scale': round(scale, 4),
                    },
                )
            except Exception:
                pass


