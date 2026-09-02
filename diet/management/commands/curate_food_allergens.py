"""
Curate FoodItem allergen data.

Three outcomes per food:

  ``verified`` + ``allergens=[]``   a single-ingredient whole food (produce, plain meat,
                                    plain grain, plain legume, pure oil). These carry
                                    none of the 14 major allergens, and saying so is a
                                    statement we can actually stand behind.
  ``inferred`` + tags               a marker was detected in the name. Real, but a hint —
                                    promoting it to `verified` is a human decision.
  ``unknown``                       composite, branded or opaque. Left alone ON PURPOSE.

Marking everything `verified` would be worse than the original bug: `Halibut`,
`Farro`, `Chicken Tikka`, `Granola Bar` and `Manual Food 7` would all be stamped
"contains no allergens", and the checker would then confidently serve them to someone
who declared that allergy. Silence is recoverable; a false clearance is not.

    python manage.py curate_food_allergens            # report only
    python manage.py curate_food_allergens --apply    # write
"""

import re

from django.core.management.base import BaseCommand

from diet.allergens import infer_allergens
from diet.models import FoodItem

# Single-ingredient whole foods. Matched as whole words against the full name; every
# word of the name must be either one of these or an innocuous qualifier.
WHOLE_FOODS = {
    # produce
    "apple", "apricot", "avocado", "banana", "beet", "berry", "blackberry", "blueberry",
    "broccoli", "cabbage", "cantaloupe", "carrot", "cauliflower", "celery", "cherry",
    "cucumber", "date", "eggplant", "fig", "garlic", "grape", "grapefruit", "guava",
    "honeydew", "kale", "kiwi", "leek", "lemon", "lettuce", "lime", "mango", "melon",
    "mushroom", "nectarine", "olive", "onion", "orange", "papaya", "peach", "pear",
    "pepper", "pineapple", "plum", "pomegranate", "potato", "pumpkin", "radish",
    "raspberry", "romaine", "spinach", "squash", "strawberry", "tangerine", "tomato",
    "watermelon", "zucchini", "asparagus", "artichoke", "arugula", "chard", "collard",
    "cress", "endive", "fennel", "jicama", "okra", "parsnip", "rhubarb", "shallot",
    "sprout", "turnip", "yam", "butternut", "bell", "longan", "mint", "chervil",
    "parsley", "cilantro", "basil", "thyme", "rosemary", "oregano", "sage", "dill",
    "blackberry", "blueberry", "brussels", "sprout", "coconut", "cranberry",
    "currant", "gooseberry", "lychee", "passionfruit", "persimmon", "quince",
    "apricot", "avocado", "clementine", "mandarin", "cabbage", "scallion",
    "chive", "watercress", "bok", "choy", "swede", "kohlrabi", "salsify",
    # plain proteins
    "beef", "bison", "chicken", "duck", "goat", "lamb", "pork", "rabbit", "steak",
    "sirloin", "tenderloin", "brisket", "drumstick", "thigh", "breast", "liver",
    "turkey", "veal", "venison", "porterhouse",
    # plain grains / legumes / seeds
    "rice", "quinoa", "millet", "amaranth", "buckwheat", "corn", "polenta", "grit",
    "lentil", "chickpea", "bean", "pea", "soy", "chia", "flax", "flaxseed", "sunflower",
    "pumpkinseed", "basmati", "jasmine", "arborio", "carnaroli",
    # pure fats / sweeteners
    "oil", "honey", "vinegar", "salt", "water",
}

# Words that do not change what a food IS.
QUALIFIERS = {
    "raw", "cooked", "fresh", "dried", "frozen", "canned", "whole", "lean", "skinless",
    "boneless", "grilled", "baked", "roasted", "steamed", "boiled", "plain", "organic",
    "red", "green", "yellow", "white", "black", "brown", "large", "small", "medium",
    "extra", "virgin", "light", "dark", "sliced", "chopped", "diced", "ground", "meat",
    "skin", "and", "or", "of", "the", "with", "without", "no", "low", "high", "free",
    "juice", "leg", "seed", "seeds", "s", "cut", "piece", "pieces", "half", "whole",
    "broiler", "broilers", "fryer", "fryers", "oz", "lb", "g", "kg", "romaine",
    "cherry", "baby", "mini", "jumbo", "ripe", "peeled", "unsalted", "salted",
}

_WORD = re.compile(r"[a-z]+")


def classify(name: str):
    """Return (source, tags, reason)."""
    tags = sorted(infer_allergens(name))
    if tags:
        return "inferred", tags, "allergen marker detected"

    words = _WORD.findall((name or "").lower())
    if not words:
        return "unknown", [], "empty name"
    # A placeholder like "Manual Food 7" carries no information at all.
    if "manual" in words and "food" in words:
        return "unknown", [], "placeholder row — contents unknown"

    def sing(w):
        return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w

    # Keep word and singular together so the two never fall out of step.
    core = [(w, sing(w)) for w in words
            if w not in QUALIFIERS and sing(w) not in QUALIFIERS and not w.isdigit()]
    if not core:
        return "unknown", [], "only qualifiers, no identifiable food"
    if all(w in WHOLE_FOODS or sg in WHOLE_FOODS for w, sg in core):
        return "verified", [], "single-ingredient whole food"
    return "unknown", [], "composite/branded — needs a human"


class Command(BaseCommand):
    help = "Curate FoodItem allergen tags. Use --apply to write."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="write changes")
        parser.add_argument("--show-review", action="store_true",
                            help="list every food still needing a human")

    def handle(self, *args, **opts):
        buckets = {"verified": [], "inferred": [], "unknown": []}
        to_save = []
        for food in FoodItem.objects.all():
            source, tags, reason = classify(food.name or "")
            buckets[source].append((food.name, tags, reason))
            if food.allergen_source != source or sorted(food.allergens or []) != tags:
                food.allergen_source = source
                food.allergens = tags
                to_save.append(food)

        total = sum(len(v) for v in buckets.values())
        self.stdout.write(f"\n{total} food items classified:")
        self.stdout.write(f"  verified allergen-free : {len(buckets['verified'])}")
        self.stdout.write(f"  allergens inferred     : {len(buckets['inferred'])}")
        self.stdout.write(f"  needs human review     : {len(buckets['unknown'])}")

        if opts["show_review"]:
            self.stdout.write("\nStill needing a human:")
            for name, _t, reason in sorted(buckets["unknown"]):
                self.stdout.write(f"  {name:44} {reason}")

        if opts["apply"]:
            FoodItem.objects.bulk_update(to_save, ["allergens", "allergen_source"], batch_size=200)
            self.stdout.write(self.style.SUCCESS(f"\napplied to {len(to_save)} rows"))
        else:
            self.stdout.write(f"\n(dry run — {len(to_save)} rows would change; pass --apply)")
