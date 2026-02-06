"""
Utility helpers for nutrition-related conversions and fuzzy matching.

These functions are extracted from the previous monolithic ai_services.py to
enable reuse across services and to simplify testing.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Iterable, Optional, Dict, Tuple, Union
from django.conf import settings


Quantity = Union[str, float, int]


def convert_to_grams(quantity: Quantity) -> float:
    """
    Convert a quantity (number or textual with units) into grams.

    Supported units include: kg, g, lb, oz, cup, cups, tablespoon/tbsp,
    teaspoon/tsp. If parsing fails, defaults to 100.0 grams.
    """
    try:
        # Fast path for numeric values
        if isinstance(quantity, (int, float)):
            return float(quantity)

        if not isinstance(quantity, str):
            return 100.0

        txt = quantity.strip().lower()
        # Extract first numeric value if present
        import re
        match = re.findall(r"\d+\.?\d*", txt)
        val = float(match[0]) if match else 100.0

        if "kg" in txt:
            return val * 1000.0
        if " g" in txt or txt.endswith("g"):
            return val
        if "lb" in txt:
            return val * 453.592
        if "oz" in txt:
            return val * 28.3495
        if "cup" in txt:
            return val * 240.0
        if "tablespoon" in txt or "tbsp" in txt:
            return val * 15.0
        if "teaspoon" in txt or "tsp" in txt:
            return val * 5.0

        # Fallback: treat as grams if only a number is present
        return val
    except Exception:
        return 100.0


def is_piece_food_name(food_name_lower: str, piece_weights: Dict[str, float]) -> Optional[str]:
    """
    Detect if the food name represents a piece-type food (e.g., egg, banana).
    Returns the matching key from piece_weights or None.
    """
    try:
        for key in piece_weights.keys():
            if key in food_name_lower:
                return key
        # Special handling for egg synonyms
        egg_synonyms = ("poached egg", "hard-boiled", "fried egg", "scrambled egg", "egg,", "eggs")
        if any(k in food_name_lower for k in egg_synonyms):
            return "egg"
    except Exception:
        pass
    return None


def piece_based_grams_if_appropriate(
    original_quantity: Quantity,
    current_grams: float,
    food_name: str,
    piece_weights: Dict[str, float],
) -> float:
    """
    If the quantity is likely a piece count (e.g., "2" for eggs) and the
    current grams are unrealistically low, convert to grams using a per-piece
    weight from the provided piece_weights mapping.
    """
    try:
        txt = str(original_quantity).strip().lower()
        key = is_piece_food_name((food_name or "").lower(), piece_weights)
        if not key:
            return current_grams

        # Detect explicit numeric count when no real unit specified
        unit_tokens = ("kg", "g", "lb", "oz", "cup", "cups", "tablespoon", "tbsp", "teaspoon", "tsp", "ml", "l")
        import re
        nums = re.findall(r"\d+\.?\d*", txt)
        num_val = float(nums[0]) if nums else None

        if (not any(tok in txt for tok in unit_tokens)) or (
            ("g" in txt) and num_val is not None and current_grams < piece_weights.get(key, 0.0) * 0.25
        ):
            count = num_val if num_val is not None else 1.0
            return max(current_grams, count * piece_weights.get(key, 0.0))
        return current_grams
    except Exception:
        return current_grams


def find_closest_food_name(name: str, candidate_names: Iterable[str], threshold: float = 0.88) -> Optional[str]:
    """
    Fuzzy match a food name against a set/list of candidate names.
    Returns the best matching candidate (lowercased) if score >= threshold.
    """
    try:
        name_l = (name or "").lower()
        best = None
        best_ratio = 0.0
        for cand in candidate_names:
            cand_l = (cand or "").lower()
            r = SequenceMatcher(None, name_l, cand_l).ratio()
            if r > best_ratio:
                best_ratio = r
                best = cand_l
        if best and best_ratio >= threshold:
            return best
        return None
    except Exception:
        return None


# =============================
# Macro density & efficiency
# =============================

def get_macro_densities_for_food(food) -> Tuple[float, float, float, float]:
    """Return (protein_per_g, carbs_per_g, fat_per_g, kcal_per_g)."""
    try:
        p_pg = float(getattr(food, 'protein_per_gram', 0.0) or 0.0)
        c_pg = float(getattr(food, 'carbs_per_gram', 0.0) or 0.0)
        f_pg = float(getattr(food, 'fat_per_gram', 0.0) or 0.0)
        kcal_pg = float(getattr(food, 'calories_per_gram', 0.0) or 0.0)
        
        # Fallback derive per-gram if missing
        if kcal_pg <= 0.0:
            cals = float(getattr(food, 'calories', 0.0) or 0.0)
            serv_g = float(getattr(food, 'serving_size_grams', 100.0) or 100.0)
            if serv_g > 0:
                kcal_pg = cals / serv_g
        
        # If per-gram values missing, calculate from serving values
        if p_pg <= 0.0 or c_pg <= 0.0 or f_pg <= 0.0:
            serv_g = float(getattr(food, 'serving_size_grams', 100.0) or 100.0)
            if serv_g > 0:
                if p_pg <= 0.0:
                    protein = float(getattr(food, 'protein', 0.0) or 0.0)
                    p_pg = protein / serv_g
                if c_pg <= 0.0:
                    carbs = float(getattr(food, 'carbs', 0.0) or 0.0)
                    c_pg = carbs / serv_g
                if f_pg <= 0.0:
                    fat = float(getattr(food, 'fat', 0.0) or 0.0)
                    f_pg = fat / serv_g
        
        return p_pg, c_pg, f_pg, kcal_pg
    except Exception:
        return 0.0, 0.0, 0.0, 0.0


def macro_efficiency_score(protein_pg: float, carb_pg: float, fat_pg: float, goal: str) -> float:
    """Weighted macro efficiency score per gram based on goal."""
    g = (goal or 'Maintain').lower()
    if 'lose' in g:
        w = (0.6, 0.3, 0.1)
    elif 'gain' in g:
        w = (0.4, 0.5, 0.1)
    else:
        w = (0.4, 0.4, 0.2)
    return w[0] * protein_pg + w[1] * carb_pg + w[2] * fat_pg


def portion_sanity_cap_grams(dominant_macro: str) -> float:
    """Upper cap for a single component quantity in grams based on guardrails."""
    try:
        guard = getattr(settings, 'AI_CHEF_CONFIG', {}).get('PORTION_GUARDRAILS', {})
        bounds = guard.get(dominant_macro, None)
        if isinstance(bounds, (tuple, list)) and len(bounds) == 2:
            return float(bounds[1])
    except Exception:
        pass
    # Fallback upper-bounds
    return {
        'protein': 350.0,
        'carb': 400.0,
        'fat': 100.0,
    }.get(dominant_macro, 300.0)


def goal_meal_kcal_split(goal: str) -> Dict[str, float]:
    """Return per-meal kcal split fractions for Breakfast/Lunch/Dinner."""
    g = (goal or 'Maintain').lower()
    if 'lose' in g:
        return {'Breakfast': 0.30, 'Lunch': 0.40, 'Dinner': 0.30}
    if 'gain' in g:
        return {'Breakfast': 0.35, 'Lunch': 0.35, 'Dinner': 0.30}
    return {'Breakfast': 0.35, 'Lunch': 0.40, 'Dinner': 0.25}


SAFE_FALLBACK_FOODS = {
    'protein': [
        'Chicken Breast', 'Egg Whites', 'Tofu', 'Salmon', 'Tuna', 'Greek Yogurt', 'Cottage Cheese'
    ],
    'carb': [
        'Oats', 'Brown Rice', 'Sweet Potato', 'Whole Grain Bread', 'Quinoa', 'Lentils', 'Chickpeas'
    ],
    'fat': [
        'Almonds', 'Walnuts', 'Olive Oil', 'Avocado', 'Flax Seeds', 'Peanut Butter'
    ],
    'veggie': [
        'Broccoli', 'Spinach', 'Zucchini', 'Carrot', 'Bell Pepper', 'Cherry Tomato'
    ],
}


# =============================
# Centralized Macro Ratios (FIX #1)
# =============================

def get_macro_ratios(goal: str) -> Dict[str, float]:
    """
    CENTRALIZED macro ratio function.
    All files should import and use this instead of duplicating the logic.
    
    Returns dict with keys: 'protein', 'carb', 'fat' (values sum to 1.0)
    
    Ratios represent percentage of daily calories from each macro:
    - Lose: High protein (35%), moderate carbs (40%), lower fat (25%)
    - Gain: High carbs (55%), moderate protein (25%), lower fat (20%)
    - Maintain: Balanced (30% protein, 50% carbs, 20% fat)
    """
    g = (goal or 'Maintain').lower()
    if 'lose' in g or 'shred' in g or 'cut' in g:
        return {"protein": 0.35, "carb": 0.40, "fat": 0.25}
    if 'gain' in g or 'bulk' in g or 'muscle' in g:
        return {"protein": 0.25, "carb": 0.55, "fat": 0.20}
    # Default: Maintain
    return {"protein": 0.30, "carb": 0.50, "fat": 0.20}


def get_macro_priority_order(goal: str) -> list:
    """
    CENTRALIZED macro priority order based on goal.
    For Gain: Carbs first to ensure calorie surplus
    For Lose: Protein first to preserve muscle
    """
    g = (goal or 'Maintain').lower()
    if 'gain' in g or 'bulk' in g or 'muscle' in g:
        return ['carb', 'protein', 'fat']
    # Lose or Maintain: protein first
    return ['protein', 'carb', 'fat']


# =============================
# Shared Macro Utility Functions
# =============================

def dominant_macro_of_food(food: Optional[object]) -> str:
    """
    CENTRALIZED function to determine the dominant macro of a food item.
    
    All service files should import and use this instead of duplicating the logic.
    
    Args:
        food: A FoodItem model instance (or any object with category and per_gram attrs).
              Can be None for safety.
    
    Returns:
        'protein', 'carb', or 'fat' based on:
        1. Category flags (is_protein, is_carb, is_fat) - preferred
        2. Caloric contribution from per-gram values - fallback
        3. Defaults to 'carb' if no data available
    """
    if food is None:
        return 'carb'  # Safe default
    
    try:
        # Prefer category flags if available
        category = getattr(food, 'category', None)
        if category is not None:
            if getattr(category, 'is_protein', False):
                return 'protein'
            if getattr(category, 'is_carb', False):
                return 'carb'
            if getattr(category, 'is_fat', False):
                return 'fat'
    except Exception:
        pass
    
    # Fallback to caloric contribution from per-gram values
    try:
        p_pg = float(getattr(food, 'protein_per_gram', 0.0) or 0.0)
        c_pg = float(getattr(food, 'carbs_per_gram', 0.0) or 0.0)
        f_pg = float(getattr(food, 'fat_per_gram', 0.0) or 0.0)
        
        # Calculate caloric contribution per gram
        p_cals = 4.0 * p_pg
        c_cals = 4.0 * c_pg
        f_cals = 9.0 * f_pg
        
        # Return macro with highest caloric contribution
        if p_cals >= c_cals and p_cals >= f_cals:
            return 'protein'
        if c_cals >= p_cals and c_cals >= f_cals:
            return 'carb'
        return 'fat'
    except Exception:
        return 'carb'  # Safe default


def macro_per_gram(food: Optional[object], macro: str) -> float:
    """
    CENTRALIZED function to get the per-gram density of a specific macro.
    
    All service files should import and use this instead of duplicating the logic.
    
    Args:
        food: A FoodItem model instance (or any object with per_gram attrs).
              Can be None for safety.
        macro: One of 'protein', 'carb', or 'fat'
    
    Returns:
        The per-gram value for the specified macro, or 0.0 if unavailable.
    """
    if food is None:
        return 0.0
    
    try:
        if macro == 'protein':
            return float(getattr(food, 'protein_per_gram', 0.0) or 0.0)
        if macro == 'carb':
            return float(getattr(food, 'carbs_per_gram', 0.0) or 0.0)
        if macro == 'fat':
            return float(getattr(food, 'fat_per_gram', 0.0) or 0.0)
        return 0.0
    except Exception:
        return 0.0


# Alias for backward compatibility with some service files
macro_ratios_for_goal = get_macro_ratios

