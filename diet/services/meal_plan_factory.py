from __future__ import annotations

from datetime import date as date_cls, datetime, time, timedelta
from typing import Iterable, Tuple, List
from ..ai_models import AIMeal
from ..models import Meal, MealComponent, FoodItem, DietPlan
from ..utils.nutrition import convert_to_grams, piece_based_grams_if_appropriate, dominant_macro_of_food


#: When each slot is eaten. The engine decided WHAT a breakfast is and left WHEN
#: unset, so every meal reached the client with no time on it. Three consequences,
#: all of them real: the app had nothing to sort or show a schedule by; `Meal`'s
#: uniqueness key includes `scheduled_time`, and Postgres treats NULLs as distinct,
#: so the key never fired and a regenerated day could hold seven lunches; and a
#: client with two snacks had two rows the database could not tell apart.
#:
#: Times a Syrian household would recognise, and snacks are spaced so a second one
#: is the afternoon and a third the evening.
SLOT_TIMES = {
    'Breakfast': time(8, 0),
    'Lunch': time(13, 30),
    'Dinner': time(20, 0),
    'Snack': time(11, 0),
}
#: Each further meal in the same slot on the same day moves by this much.
SLOT_REPEAT_OFFSET = timedelta(hours=5)


def scheduled_time_for(slot: str, occurrence: int = 0):
    """The time this meal is eaten, given how many of its slot came before it today."""
    base = SLOT_TIMES.get(slot, SLOT_TIMES['Lunch'])
    if not occurrence:
        return base
    moved = (datetime.combine(date_cls.min, base)
             + SLOT_REPEAT_OFFSET * occurrence)
    # Never past the end of the day; pile up just before midnight rather than wrap
    # into the previous morning and change the meal's order.
    if moved.date() != date_cls.min:
        # Past midnight. Step back from 23:59 by the occurrence so repeated slots stay
        # DISTINCT — returning 23:59 for every overflow made two meals collide on the
        # uniqueness key and the whole plan was refused.
        minute = max(0, 59 - occurrence)
        return time(23, minute)
    return moved.time()


class MealPlanFactory:
    """
    Responsible for constructing `Meal` and `MealComponent` objects from AI meals.
    """

    def __init__(self, piece_weights: dict[str, float]):
        self.piece_weights = piece_weights

    def create_meal(self, diet_plan: DietPlan, ai_meal: AIMeal, date,
                    occurrence: int = 0) -> Meal:
        slot = ai_meal.meal_type or 'Lunch'
        # A recipe meal is named after its dish; an engine-built meal arrives named after
        # the slot, which is not a name. Compose one from what is on the plate.
        name = ai_meal.meal_name if ai_meal.meal_name != slot else self._compose_name(ai_meal)
        name_ar = (ai_meal.meal_name_ar if ai_meal.meal_name_ar and ai_meal.meal_name != slot
                   else self._compose_name(ai_meal, lang="ar"))
        return Meal.objects.create(
            diet_plan=diet_plan,
            template=self._determine_meal_template(ai_meal),
            date=date,
            description=ai_meal.description,
            meal_type=slot,
            is_ai_generated=True,
            scheduled_time=scheduled_time_for(slot, occurrence),
            name=name[:160],
            name_ar=name_ar[:160],
            recipe_id=ai_meal.recipe_id,
            shape=(ai_meal.shape or '')[:80],
            reason=(ai_meal.reason or '')[:200],
            reason_ar=(ai_meal.reason_ar or '')[:200],
            target=dict(ai_meal.target) if ai_meal.target else None,
            # The Arabic description, in the column's documented place for it. It was
            # English only (BACKEND_BUGS B-063); `meal_payload` reads it as
            # `description_ar`.
            translations=({"ar": {"description": ai_meal.description_ar}}
                          if ai_meal.description_ar else {}),
        )

    @staticmethod
    def _compose_name(ai_meal: AIMeal, lang: str = "en") -> str:
        """"Chicken, Rice and Broccoli" from the components; "دجاج، أرز وبروكلي" in Arabic."""
        from diet.planner.recipes import plain_name

        if lang == "ar":
            names = [ing.name_ar or ing.name for ing in ai_meal.ingredients][:4]
            if not names:
                return "وجبة"
            if len(names) == 1:
                return names[0]
            return "، ".join(names[:-1]) + " و" + names[-1]
        # Without the catalogue qualifier: "Cottage Cheese, Whole Wheat Bread, Green
        # Peas and Apple", not "Cottage Cheese (Low-Fat), ... Green Peas (Cooked) ...".
        names = [plain_name(ing) for ing in ai_meal.ingredients][:4]
        if not names:
            return ai_meal.meal_type or 'Meal'
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " and " + names[-1]

    def add_components(self, meal: Meal, resolved: Iterable[Tuple[FoodItem, str]]) -> None:
        # Components created in THIS call, by food id. The meal is new, so a food can
        # only already be on it if this loop put it there; asking the database per
        # component was a query that always answered "no" on the planner's path.
        added: dict = {}
        pending: list = []
        for food_item, quantity in resolved:
            if isinstance(quantity, (int, float)):
                # A gram figure the planner decided. It is not text to be parsed and not
                # a piece count to be reinterpreted.
                grams = float(quantity)
            else:
                grams = convert_to_grams(quantity)
                grams = piece_based_grams_if_appropriate(
                    quantity, grams, getattr(food_item, "name_en", None) or food_item.name,
                    self.piece_weights)

            # The SAME food twice on one plate is one portion split in two, so the grams
            # are summed. Two DIFFERENT foods are two foods.
            #
            # There used to be a third rule between those: a normalised-name match, where
            # `_normalize_food_name` mapped 'chicken breast' and 'chicken thigh' both to
            # 'chicken', and 'brown rice' and 'white rice' both to 'rice'. A meal holding
            # two of them was persisted as one component with the summed grams — then
            # clamped to a cap, losing the calories too — and the second food was never
            # written at all. The client was shown a meal that was never generated, and
            # `DailyProgress` summed the wrong food.
            existing = added.get(food_item.id)

            if existing:
                existing.quantity = float(existing.quantity or 0.0) + float(grams or 0.0)
                # Clamp per-item cap
                from ..utils.nutrition import portion_sanity_cap_grams
                dom = dominant_macro_of_food(existing.food)  # Use existing food for consistency
                cap = portion_sanity_cap_grams(dom)
                if dom == 'carb':
                    cap = min(cap, 400.0)
                if dom == 'protein':
                    cap = min(cap, 350.0)
                if dom == 'fat':
                    cap = min(cap, 100.0)
                name_l = (getattr(existing.food, "name_en", None) or existing.food.name or '').lower()
                veg_keywords = ('lettuce','tomato','tomatoes','cucumber','green bean',
                              'spinach','zucchini','broccoli','asparagus','carrot',
                              'pepper','cabbage','cauliflower','celery')
                if any(k in name_l for k in veg_keywords):
                    cap = min(cap, 300.0)
                if existing.quantity > cap:
                    existing.quantity = cap
                if existing.pk:
                    existing.save(update_fields=['quantity'])
                # else: it is still in `pending` and will be written with the merged
                # quantity when the batch goes in.
            else:
                row = MealComponent(
                    meal=meal,
                    food=food_item,
                    quantity=grams,
                    meal_time=meal.meal_type,
                )
                added[food_item.id] = row
                pending.append(row)

        # One INSERT for the meal's components, not one per component. A seven-day
        # plan wrote 127 of them. `MealComponent` has no custom save(), and the only
        # receiver on it returns early unless the row is completed — which a new row
        # never is — so nothing is skipped by writing them together.
        if pending:
            MealComponent.objects.bulk_create(pending)

    def _determine_meal_template(self, ai_meal: AIMeal) -> str:
        nutrition = ai_meal.total_nutrition
        protein = nutrition.get('protein', 0)
        carbs = nutrition.get('carbs', 0)
        fat = nutrition.get('fat', 0)
        if protein > carbs and protein > fat:
            return 'PROTEIN_CARB' if carbs > fat else 'PROTEIN_FAT'
        elif carbs > protein and carbs > fat:
            return 'CARB_FAT' if fat > protein else 'PROTEIN_CARB'
        else:
            return 'COMPLETE'
    
