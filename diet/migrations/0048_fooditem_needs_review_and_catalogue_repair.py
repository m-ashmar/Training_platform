"""Repair the food catalogue, and give it a way to say "these numbers are wrong".

`FoodItem.clean()` states real rules about nutrition, and 99 of 346 rows break them.
Until now nothing ran those rules, so the rows sat there and the planner portioned
from them. Three different problems were hiding under one number:

* **44 rows store `/media/placeholder_food.jpg` in a URLField.** A relative path is
  not a URL, and the mobile client cannot resolve it as one either. The absence of a
  picture is null, so write null.
* **52 rows label a 100 g serving 'Serving' or leave it blank.** Their macros are
  Edamam's per-100 g figures and are correct — Arborio rice at 358 kcal, bacon fat at
  897 — so `serving_size_grams = 100` was never a guess here, only the label was
  wrong. Say 100g, like the other 247 rows.
* **A handful state calories their own macros contradict**, by more than the 35% the
  model allows: brick cheese at 1200 kcal against 371 from its macros, avocado oil at
  9.29 kcal/g when pure fat tops out at 9.1. These need a person. Rewriting them from
  the macros would be inventing nutrition, so they get flagged instead: `needs_review`
  keeps them out of the planner's pool and keeps them saveable while someone fixes
  them.

One row also carries an Edamam id and no name at all. It is referenced by a meal, so
it cannot simply go; it gets a name that says what it is and the same flag.
"""
from django.db import migrations, models

ATWATER_TOLERANCE = 0.35
MAX_KCAL_PER_GRAM = 9.1


def repair(apps, schema_editor):
    FoodItem = apps.get_model("diet", "FoodItem")

    # A placeholder path is not a URL and not an image.
    FoodItem.objects.filter(image_url__startswith="/").update(image_url=None)

    # The weight was right; only the label was missing.
    FoodItem.objects.filter(
        serving_size__in=("", "Serving"), serving_size_grams=100
    ).update(serving_size="100g")

    flagged = []
    for food in FoodItem.objects.all().iterator(chunk_size=500):
        cal = float(food.calories or 0)
        grams = food.serving_size_grams or 0
        atwater = 4 * float(food.protein or 0) + 4 * float(food.carbs or 0) + 9 * float(food.fat or 0)
        bad = False
        if grams > 0 and cal > 0 and cal / grams > MAX_KCAL_PER_GRAM:
            bad = True
        if cal > 0 and atwater > 0 and abs(atwater - cal) / cal > ATWATER_TOLERANCE:
            bad = True
        if not (food.name or "").strip():
            food.name = f"Unnamed import ({food.api_id or food.pk})"
            bad = True
        if not (food.api_id or "").strip():
            food.api_id = f"manual-{food.pk}"
        if bad:
            food.needs_review = True
        if bad or not (food.api_id or "").strip():
            flagged.append(food)
    if flagged:
        FoodItem.objects.bulk_update(flagged, ["name", "api_id", "needs_review"])


def unrepair(apps, schema_editor):
    """Only the flag is reversible; the repaired values are the correct ones."""
    apps.get_model("diet", "FoodItem").objects.update(needs_review=False)


class Migration(migrations.Migration):
    dependencies = [("diet", "0047_dietplan_allergen_report")]
    operations = [
        migrations.AddField(
            model_name="fooditem",
            name="needs_review",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Nutrition on this row failed a sanity check and a human has not "
                    "yet corrected it. The planner will not portion from it."
                ),
            ),
        ),
        migrations.RunPython(repair, unrepair),
    ]
