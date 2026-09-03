"""Finish what 0048 started, on the three rows it did not reach.

Three separate reasons it missed them, each worth naming:

* **A name written in a data migration does not reach the translated column.**
  `apps.get_model()` returns a historical model, and modeltranslation is not
  registered against it, so assigning `name` wrote only the plain column while the
  API — which reads `name_en` for an English request — still saw blank. This is the
  same shape as the import that once left 542 of 554 exercise names empty through the
  API: the row looks right in the database and reads empty through Django.

* **A blank `api_id` was repaired and then not saved.** 0048 assigned the value and
  then tested the field it had just assigned before deciding whether to include the
  row in the update, so the row it had fixed was the one row it excluded.

* **'Whole' is a serving label with no number in it, and 0048 only knew 'Serving'.**
  Match on the absence of a digit rather than on a list of words nobody can finish.
"""
from django.db import migrations


def finish(apps, schema_editor):
    FoodItem = apps.get_model("diet", "FoodItem")

    # Any label carrying no number, over the default weight, is per-100 g like the rest.
    FoodItem.objects.filter(serving_size_grams=100).exclude(
        serving_size__regex=r"[0-9]"
    ).update(serving_size="100g")

    for food in FoodItem.objects.filter(api_id=""):
        FoodItem.objects.filter(pk=food.pk).update(api_id=f"manual-{food.pk}")

    # Write the translated column too, or the API keeps reading blank.
    for food in FoodItem.objects.filter(name_en__in=("", None)).exclude(name=""):
        FoodItem.objects.filter(pk=food.pk).update(name_en=food.name)


def noop(apps, schema_editor):
    """The repaired values are the correct ones; there is nothing to restore."""


class Migration(migrations.Migration):
    dependencies = [("diet", "0049_retire_overlapping_active_plans")]
    operations = [migrations.RunPython(finish, noop)]
