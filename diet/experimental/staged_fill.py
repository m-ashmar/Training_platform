from __future__ import annotations

from typing import Dict, List, Tuple
from dataclasses import dataclass

from django.conf import settings  # noqa: F401 (reserved for future staged feature flags)
import random

from diet.models import FoodItem
from diet.utils.nutrition import portion_sanity_cap_grams  # noqa: F401 (kept for documentation parity)
from diet.utils.portioning import compute_portion_grams
import logging

logger = logging.getLogger(__name__)


@dataclass
class MealTargets:
    kcal_target: float
    protein_g: float
    carb_g: float
    fat_g: float


class StagedMealFiller:
    """Protein → Carbs → Fats staged filler with portion caps/floors.

    This module is intentionally decoupled so we can iterate without touching core planner code.
    """

    def __init__(self, min_caps: Dict[str, float] | None = None, max_caps: Dict[str, float] | None = None):
        # Portion floors/ceilings for item grams
        self.min_caps = min_caps or {"protein": 100.0, "carb": 40.0, "fat": 20.0}
        self.max_caps = max_caps or {"protein": 350.0, "carb": 400.0, "fat": 80.0}

    def fill(self,
             meal_type: str,
             targets: MealTargets,
             pools: Dict[str, List[FoodItem]],
             recent_ids: set[int] | None = None,
             goal: str = 'maintain') -> List[Tuple[FoodItem, float]]:
        recent_ids = recent_ids or set()
        components: List[Tuple[FoodItem, float]] = []
        residual = {"protein": max(0.0, targets.protein_g),
                    "carb": max(0.0, targets.carb_g),
                    "fat": max(0.0, targets.fat_g)}
        kcal_consumed = 0.0

        def add_item(food: FoodItem, grams: float):
            nonlocal kcal_consumed
            # Prevent duplicates of the same food within a meal
            for f_existing, _g in components:
                if getattr(f_existing, 'id', None) == getattr(food, 'id', None):
                    return
            components.append((food, grams))
            kcal_pg = float(getattr(food, 'calories_per_gram', 0.0) or 0.0)
            p_pg = float(getattr(food, 'protein_per_gram', 0.0) or 0.0)
            c_pg = float(getattr(food, 'carbs_per_gram', 0.0) or 0.0)
            f_pg = float(getattr(food, 'fat_per_gram', 0.0) or 0.0)
            kcal_consumed += grams * kcal_pg
            residual["protein"] = max(0.0, residual["protein"] - grams * p_pg)
            residual["carb"] = max(0.0, residual["carb"] - grams * c_pg)
            residual["fat"] = max(0.0, residual["fat"] - grams * f_pg)
            try:
                print(f"[RESIDUAL] meal={meal_type} add={getattr(food,'name','')} {int(grams)}g -> kcal_left={round(max(0.0, float(targets.kcal_target or 0.0) - kcal_consumed),1)} residual={{'protein':round(residual['protein'],1),'carb':round(residual['carb'],1),'fat':round(residual['fat'],1)}}")
            except Exception:
                # Optional side effect: swallowing this silently is what made the
                # surrounding failures invisible in logs. Control flow is unchanged.
                logger.debug('suppressed non-fatal error', exc_info=True)

        # Stage helper
        def stage(macro: str):
            items = [f for f in pools.get(macro, []) if f.id not in recent_ids]
            if not items:
                return
            # Pick best macro-dominant item by macro density per kcal
            def density_per_kcal(f: FoodItem) -> float:
                pg = getattr(f, f"{macro}s_per_gram" if macro == 'carb' else f"{macro}_per_gram", 0.0)
                kcal_pg = float(getattr(f, 'calories_per_gram', 0.0) or 0.0)
                pg = float(pg or 0.0)
                if kcal_pg <= 0.0:
                    return 0.0
                return pg / kcal_pg
            items.sort(key=density_per_kcal, reverse=True)
            # Optionally randomize among the top-K to add variety across runs while keeping quality
            if getattr(settings, 'DIIET_RANDOM_SELECTION', None) is not None:
                # Backward compatible typo guard; prefer DIET_RANDOM_SELECTION but accept DIIET_ if present
                randomize = bool(getattr(settings, 'DIIET_RANDOM_SELECTION'))
            else:
                randomize = getattr(settings, 'DIET_RANDOM_SELECTION', True)
            if randomize and len(items) > 1:
                k = min(len(items), max(1, int(getattr(settings, 'DIET_RANDOM_TOP_K', 3))))
                head, tail = items[:k], items[k:]
                random.shuffle(head)
                items = head + tail
            # Only 1 protein item per meal; carbs up to 2; fat 1
            if macro == 'protein':
                take = min(1, len(items))
            elif macro == 'fat':
                take = min(1, len(items))
            else:
                take = min(2, len(items))
            for f in items[:take]:
                # Compute grams using shared portioning logic
                kcal_res = max(0.0, float(targets.kcal_target or 0.0) - kcal_consumed)
                grams = compute_portion_grams(
                    food=f,
                    macro=macro,
                    remaining_macro_g=residual[macro],
                    remaining_kcal=kcal_res,
                    goal=goal,
                    gram_cap_override=400.0 if macro == 'carb' else None,
                    carb_variable=True,
                    piece_weights=None,
                )
                # If there's no feasible grams, skip (do NOT force a floor that breaks kcal budget)
                if grams <= 0.0:
                    continue
                # Respect staged floors without exceeding remaining kcal budget
                kcal_pg = float(getattr(f, 'calories_per_gram', 0.0) or 0.0)
                # Dynamic fat floor: disable if residual fat need is small or kcal is tight
                local_floor = float(self.min_caps.get(macro, 0.0) or 0.0)
                if macro == 'fat':
                    fat_needed = float(residual.get('fat', 0.0) or 0.0)
                    if fat_needed < 5.0 or kcal_res < 50.0:
                        local_floor = 0.0
                        try:
                            print(f"[FAT_FLOOR] meal={meal_type} disabled (need={round(fat_needed,1)}g, kcal_left={round(kcal_res,1)})")
                        except Exception:
                            # Optional side effect: swallowing this silently is what made the
                            # surrounding failures invisible in logs. Control flow is unchanged.
                            logger.debug('suppressed non-fatal error', exc_info=True)
                    else:
                        try:
                            print(f"[FAT_FLOOR] meal={meal_type} floor={round(local_floor,1)} (need={round(fat_needed,1)}g, kcal_left={round(kcal_res,1)})")
                        except Exception:
                            # Optional side effect: swallowing this silently is what made the
                            # surrounding failures invisible in logs. Control flow is unchanged.
                            logger.debug('suppressed non-fatal error', exc_info=True)
                if kcal_pg > 0.0:
                    if local_floor > 0.0 and grams < local_floor:
                        max_by_kcal = kcal_res / kcal_pg if kcal_res > 0.0 else 0.0
                        # Do not exceed budget when raising to floor
                        grams = min(max(local_floor, grams), max_by_kcal) if max_by_kcal > 0.0 else grams
                add_item(f, grams)

        # Stage order: protein → carb → fat
        stage('protein')
        stage('carb')
        # Skip fat if we already placed protein and carb and remaining fat target is tiny
        try:
            have_protein = (targets.protein_g - residual['protein']) > 0.0
            have_carb = (targets.carb_g - residual['carb']) > 0.0
            rem_fat = max(0.0, residual['fat'])
            if have_protein and have_carb and rem_fat < 10.0:
                print(f"[FAT_SKIP_STAGED] meal={meal_type} remaining_fat={round(rem_fat,1)}g < 10g, skipping fat stage")
            else:
                stage('fat')
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)
        return components


