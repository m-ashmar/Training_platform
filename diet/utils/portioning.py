from __future__ import annotations

from typing import Optional

import random
import math

from diet.models import FoodItem, DietConfig
from diet.utils.nutrition import (
    get_macro_densities_for_food,
    portion_sanity_cap_grams,
    is_piece_food_name,
)


def _is_oil_name(name: str) -> bool:
    try:
        n = (name or "").strip().lower()
    except Exception:
        n = ""
    return "oil" in n


def _is_vegetable_food(food: FoodItem) -> bool:
    try:
        cat = getattr(food, "category", None)
        if cat and hasattr(cat, "name"):
            nm = (getattr(cat, "name", "") or "").lower()
            if "vegetable" in nm or "vegetables" in nm or "veggie" in nm:
                return True
    except Exception:
        pass
    name = ((getattr(food, "name", "") or "")).lower()
    veg_keywords = (
        "lettuce",
        "tomato",
        "tomatoes",
        "cucumber",
        "green bean",
        "spinach",
        "zucchini",
        "broccoli",
        "asparagus",
        "carrot",
        "pepper",
        "cabbage",
        "cauliflower",
        "celery",
        "kale",
        "brussels sprout",
        "brussels sprouts",
    )
    return any(k in name for k in veg_keywords)


def _round_grams_half_up_to_5(grams: float) -> float:
    if grams <= 0.0:
        return 0.0
    return float((math.floor((grams + 2.5) / 5.0)) * 5.0)


def _snap_pieces_if_needed(food: FoodItem, grams: float, piece_weights: Optional[dict]) -> float:
    try:
        pw_map = piece_weights
        if pw_map is None:
            cfg = DietConfig.objects.last()
            pw_map = (cfg.piece_weights if cfg and cfg.piece_weights else {})
        key = is_piece_food_name((food.name or "").lower(), pw_map)
        if key:
            pw = float(pw_map.get(key, 0.0) or 0.0)
            if pw > 0.0:
                pieces = max(1, int(round(grams / pw)))
                return float(pieces * pw)
    except Exception:
        pass
    return grams


def compute_portion_grams(
    food: FoodItem,
    macro: str,
    remaining_macro_g: float,
    remaining_kcal: float,
    goal: str,
    gram_cap_override: float | None = None,
    carb_variable: bool = True,
    piece_weights: Optional[dict] = None,
) -> float:
    """
    Shared portion sizing logic for staged fill and macro loop.
    Applies consistent caps: oil/fat, vegetable, sanity, piece-snapping, rounding.
    Emits debug prints to explain calculation steps.
    """
    try:
        print(
            f"[PORTION] start food={getattr(food,'name','')} macro={macro} rem_macro_g={round(remaining_macro_g,2)} rem_kcal={round(remaining_kcal,1)} cap_override={gram_cap_override}"
        )
    except Exception:
        pass

    # Per-gram densities
    macro_per_g = float(getattr(food, f"{'carbs' if macro=='carb' else macro}_per_gram", 0.0) or 0.0)
    kcal_per_g = float(getattr(food, "calories_per_gram", 0.0) or 0.0)
    if remaining_kcal <= 0.0:
        try:
            print(f"[PORTION] skip (no kcal budget) mpg={macro_per_g} kpg={kcal_per_g}")
        except Exception:
            pass
        return 0.0
    if macro_per_g <= 0.0 or kcal_per_g <= 0.0:
        try:
            print(f"[PORTION] skip (non-positive mpg/kpg) mpg={macro_per_g} kpg={kcal_per_g}")
        except Exception:
            pass
        return 0.0

    grams_for_macro = remaining_macro_g / macro_per_g if macro_per_g > 0 else 0.0
    grams_for_kcal = remaining_kcal / kcal_per_g if kcal_per_g > 0 else grams_for_macro
    grams = max(0.0, min(grams_for_macro, grams_for_kcal))
    try:
        print(
            f"[PORTION] base grams_for_macro={round(grams_for_macro,1)} grams_for_kcal={round(grams_for_kcal,1)} grams_base={round(grams,1)} (mpg={round(macro_per_g,4)} kpg={round(kcal_per_g,4)})"
        )
    except Exception:
        pass

    # Explicit cap override
    if gram_cap_override is not None:
        grams = min(grams, float(gram_cap_override))
        try:
            print(f"[PORTION] gram_cap_override -> {round(grams,1)}")
        except Exception:
            pass

    # Oil/fat caps
    if macro == "fat" and _is_oil_name(getattr(food, "name", "") or ""):
        grams = min(grams, 15.0)
        try:
            print(f"[PORTION] oil cap -> {round(grams,1)}")
        except Exception:
            pass
    if macro == "fat":
        grams = min(grams, 50.0)
        try:
            print(f"[PORTION] fat cap -> {round(grams,1)}")
        except Exception:
            pass

    # Portion sanity by targeted macro (not food dominance)
    grams = min(grams, portion_sanity_cap_grams(macro))
    try:
        print(f"[PORTION] sanity cap ({macro}) -> {round(grams,1)}")
    except Exception:
        pass

    # Carb variability cap (deterministic, target-aware, disabled when kcal-limited)
    if carb_variable and macro == "carb":
        kcal_limited = grams_for_kcal < grams_for_macro - 1e-6
        if kcal_limited:
            try:
                print(f"[PORTION] carb cap skipped (kcal-limited)")
            except Exception:
                pass
        else:
            target_cap = 1.10 * grams_for_macro  # allow mild overshoot relative to target need
            # Apply hard ceilings
            target_cap = min(target_cap, portion_sanity_cap_grams("carb"))
            if gram_cap_override is not None:
                target_cap = min(target_cap, float(gram_cap_override))
            grams = min(grams, target_cap)
            try:
                print(f"[PORTION] carb cap (target-aware) -> {round(grams,1)} (cap={round(target_cap,1)})")
            except Exception:
                pass

    # Vegetable cap: 300 g for true vegetables
    if _is_vegetable_food(food):
        grams = min(grams, 300.0)
        try:
            print(f"[PORTION] vegetable cap -> {round(grams,1)}")
        except Exception:
            pass

    # Round + snap to piece
    grams = _round_grams_half_up_to_5(grams)
    grams = _snap_pieces_if_needed(food, grams, piece_weights)
    try:
        print(f"[PORTION] final grams -> {round(grams,1)}")
    except Exception:
        pass
    return grams


