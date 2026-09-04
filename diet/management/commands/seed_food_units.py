"""Give the catalogue a serving and a role.

A portion was an unbounded continuous gram figure because nothing said what a serving
was: `serving_size` reads "100g" for almost every row and is the basis the macros are
quoted against, not an amount anyone eats. So the planner produced 350 g of egg white,
which is eleven of them, and 370 g of butternut squash.

Two things are assigned here, both by pattern rather than by an exhaustive list, so a
food imported tomorrow is covered without editing this file:

**unit** — what one of these is to a person, and how many of them is a serving. A
portion then becomes a multiple of that unit inside a declared range, which makes an
absurd plate unrepresentable rather than merely discouraged.

**role** — whether a meal can be built on this food, served alongside it, or only
seasoned with it. Ranking by grams of macro per kcal is maximised by things that are
almost pure macro and nothing else, which is how BBQ sauce came to outrank rice as a
carbohydrate. Role is what stops that, and it is a truer fix than tuning the weight,
because density is not wrong — it is answering a different question.

    python manage.py seed_food_units            # report only
    python manage.py seed_food_units --apply
"""
import re

from django.core.management.base import BaseCommand

from diet.models import FoodItem

# (pattern, household_unit, unit_grams, min_units, max_units)
# Ordered: the first match wins, so put the specific before the general.
#
# Ceilings are what a large person eats at one sitting, not what is merely possible.
# Set too low they become a different kind of wrong: capping every staple meant a
# 3,000 kcal day could not be reached at all and landed 12% under, which is the same
# failure as an absurd portion seen from the other side.
#
# Patterns deliberately open without \b and close with it. A leading boundary made
# "Blackberries" miss "berries" and "Almonds" miss "almond", because those are one word.
# The trailing boundary is what still keeps "Butternut Squash" out of the butter rule,
# which is the failure this whole file exists to stop repeating.
# A cup is a cup of whatever the row actually holds, and this catalogue stores grains
# and legumes COOKED. Sixty grams is a cup of dry rice and a third of a cup of cooked
# rice, so every cooked staple carried a ceiling roughly a third of a real serving and
# no dish built on one could reach its carbohydrate target — the planner then made up
# the difference by serving amounts outside the ladder entirely. Checked before the
# table below, and only when the row says cooked.
COOKED_MARKER = re.compile(r"\bcooked\b")
COOKED_RULES = [
    (r"(oats|oatmeal|porridge)\b", "cup", 234, 0.5, 1.5),
    (r"(pasta|noodles?|spaghetti|macaroni)\b", "cup", 140, 0.5, 2.5),
    (r"(quinoa|freekeh|bulgur|farro|millet|buckwheat|polenta)\b", "cup", 180, 0.5, 2),
    (r"(rice|couscous|barley)\b", "cup", 158, 0.5, 2.5),
    (r"(lentils?|chickpeas?|beans?|fava|ful|peas|corn)\b", "cup", 180, 0.5, 2),
]

UNIT_RULES = [
    # --- seasoned by the spoon ------------------------------------------------
    (r"(salt|pepper corns?|seasonings?|spices?)\b", "pinch", 2, 1, 2),
    (r"(garlic|ginger|chill?i(es)?|horseradish|wasabi)\b", "clove", 5, 1, 3),
    (r"(parsley|mint|coriander|cilantro|dill|basil|thyme|oregano|rosemary|sage|"
     r"chives?|herbs?|zaatar|za'atar|sumac)\b", "sprig", 5, 1, 3),
    (r"(soy sauce|vinegars?|hot sauce|mustard|ketchup)\b", "tbsp", 15, 0.5, 2),
    (r"(sauces?|dressings?|mayo|jell(y|ies)|jams?|syrups?|honey)\b", "tbsp", 20, 1, 2),
    # --- fats -----------------------------------------------------------------
    (r"(almond butter|peanut butter|tahini)\b", "tbsp", 16, 0.5, 2),
    (r"oils?\b", "tbsp", 14, 0.5, 2),
    (r"(butter|ghee)\b", "tbsp", 14, 0.5, 2),
    (r"(almonds?|walnuts?|cashews?|pistachios?|hazelnuts?|pecans?|nuts)\b",
     "handful", 28, 0.5, 2),
    (r"(seeds?|flaxseeds?|chia)\b", "tbsp", 12, 1, 3),
    (r"avocados?\b", "avocado", 200, 0.25, 0.75),
    (r"olives?\b", "serving", 30, 0.5, 2),
    # --- proteins -------------------------------------------------------------
    (r"egg whites?\b", "egg white", 33, 2, 6),
    (r"eggs?\b", "egg", 50, 1, 4),
    (r"(chicken|turkey|duck|beef|lamb|pork|bison|veal|sirloin|steak|venison|"
     r"rabbit|goat|quail)\b",
     "palm", 120, 1, 2.5),
    (r"(salmon|tuna|cod|tilapia|shrimps?|fish|sardines?|mackerel|prawns?|halibut|"
     r"sea bass|bass|trout|haddock|snapper|trevally|sole)\b",
     "fillet", 120, 1, 2.5),
    (r"(yogurt|yoghurt|labneh)\b", "pot", 150, 0.5, 2),
    (r"(cheese|halloumi|feta|cottage|mozzarella|ricotta)\b", "slice", 30, 1, 4),
    (r"(tofu|tempeh|seitan)\b", "block", 100, 0.5, 2),
    # --- carbohydrates --------------------------------------------------------
    (r"(oats|oatmeal|granola|muesli)\b", "cup", 40, 1, 3),
    (r"(rice|quinoa|freekeh|bulgur|couscous|barley|farro|millet|buckwheat|"
     r"polenta|semolina)\b", "cup", 60, 1, 3),
    (r"(lentils?|chickpeas?|beans?|fava|ful|peas)\b", "cup", 100, 0.5, 3),
    (r"(bread|pita|toast|bagel|manakish|tortillas?|cracker|muffins?)\b",
     "slice", 40, 1, 3),
    (r"(pasta|noodles?|spaghetti|macaroni)\b", "cup", 75, 1, 2),
    (r"(potato(es)?|yams?)\b", "medium", 150, 0.5, 2.5),
    (r"(edamame|sprouted)\b", "cup", 155, 0.5, 1.5),
    # --- produce --------------------------------------------------------------
    (r"(bananas?|apples?|oranges?|pears?|peach(es)?|mangos?|kiwis?|plums?)\b",
     "medium", 120, 0.5, 2),
    (r"(berry|berries|grapes?|cherry|cherries)\b", "cup", 100, 0.5, 2),
    (r"(melons?|cantaloupe|pineapples?|papayas?)\b", "slice", 120, 1, 2),
    (r"(dates?|figs?|apricots?|raisins?|prunes?)\b", "piece", 8, 2, 6),
    (r"(spinach|kale|lettuce|romaine|arugula|chard|greens)\b", "handful", 40, 1, 3),
    (r"(broccoli|cauliflower|carrots?|cucumbers?|tomato(es)?|peppers?|zucchini|"
     r"courgettes?|asparagus|eggplants?|aubergines?|okra|squash|pumpkins?|beets?|"
     r"onions?|leeks?|mushrooms?|celery|cabbage|sprouts?)\b", "serving", 80, 0.5, 2),
]

# Opens without \b for the same reason UNIT_RULES does: "Juices" must match "juice"
# and "Sauces" must match "sauce". The trailing boundary still keeps "Butternut" clear
# of "butter".
CONDIMENT_PATTERN = re.compile(
    r"(sauces?|dressings?|jell(y|ies)|jams?|syrups?|ketchup|mayo|mustards?|"
    r"seasonings?|spices?|salt|vinegars?|colas?|sodas?|pepsi|coke|drinks?|"
    r"juices?|lemonade|-q)\b")
ACCOMPANIMENT_PATTERN = re.compile(
    r"(oils?|butter|ghee|honey|tahini|pickles?|olives?|garlic|gingers?|chill?i(es)?|"
    r"parsley|mint|coriander|cilantro|dill|basil|thyme|oregano|rosemary|sage|chives?|"
    r"herbs?|zaatar|za'atar|sumac|horseradish|wasabi)\b")


def unit_for(name: str):
    low = (name or "").lower()
    tables = ([COOKED_RULES, UNIT_RULES] if COOKED_MARKER.search(low) else [UNIT_RULES])
    for table in tables:
        for pattern, unit, grams, lo, hi in table:
            if re.search(pattern, low):
                return unit, float(grams), float(lo), float(hi)
    return None


def role_for(name: str) -> str:
    low = (name or "").lower()
    if CONDIMENT_PATTERN.search(low):
        return FoodItem.ROLE_CONDIMENT
    if ACCOMPANIMENT_PATTERN.search(low):
        return FoodItem.ROLE_ACCOMPANIMENT
    return FoodItem.ROLE_STAPLE


class Command(BaseCommand):
    help = "Assign household units and roles to the food catalogue."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write the changes. Without it, only report.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        matched = unmatched = 0
        by_role = {FoodItem.ROLE_STAPLE: 0, FoodItem.ROLE_ACCOMPANIMENT: 0,
                   FoodItem.ROLE_CONDIMENT: 0}
        misses = []
        updates = []

        for food in FoodItem.objects.all().iterator(chunk_size=500):
            role = role_for(food.name)
            by_role[role] += 1
            unit = unit_for(food.name)
            if unit is None:
                unmatched += 1
                if len(misses) < 25:
                    misses.append(food.name)
            else:
                matched += 1
            if not apply:
                continue
            food.role = role
            if unit is not None:
                food.household_unit, food.unit_grams, food.min_units, food.max_units = unit
            updates.append(food)
            if len(updates) >= 500:
                FoodItem.objects.bulk_update(
                    updates, ["role", "household_unit", "unit_grams", "min_units", "max_units"])
                updates = []
        if apply and updates:
            FoodItem.objects.bulk_update(
                updates, ["role", "household_unit", "unit_grams", "min_units", "max_units"])

        total = matched + unmatched
        self.stdout.write(f"foods: {total}")
        self.stdout.write(f"  with a unit   : {matched} ({matched / total * 100:.0f}%)"
                          if total else "  with a unit   : 0")
        self.stdout.write(f"  without       : {unmatched}")
        for role, count in by_role.items():
            self.stdout.write(f"  {role:<15}: {count}")
        if misses:
            self.stdout.write("  no unit rule matched, e.g.:")
            for name in misses[:12]:
                self.stdout.write(f"      {name}")
        self.stdout.write("applied" if apply else "dry run; pass --apply to write")
