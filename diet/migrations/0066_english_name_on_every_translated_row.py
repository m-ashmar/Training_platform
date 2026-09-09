"""Every translated row gets its English column, and 0005's two bad categories go.

Two faults, one cause: a row written before modeltranslation was wired up — or by a
migration that only ever set `name` — has `name_en = NULL`.

* `name` is a virtual field that reads `name_<active language>`, so such a row answers
  the empty string under English. Serialise the table and deserialise it (which is what
  `serialized_rollback` does, and what any `dumpdata`/`loaddata` round trip does) and
  every one of them comes back named "", colliding on `UNIQUE (name)` the moment there
  is more than one.
* `FoodCategory.by_canonical_name` has to carry a `Q(name=label)` arm purely to find
  these rows. Filling the column is the fix that arm is standing in for.

0005 also creates "Morning Proteins" and "Morning Carbs" — the CARB one flagged
`is_protein=True`. `auto_assign_category` picks a protein category with
`filter(is_protein=True).first()` under `ordering=['name_en','id']`, so "Morning Carbs"
sorts first and an imported protein is filed as a carbohydrate. They hold no foods on a
fresh install and are not referenced anywhere; remove them rather than leave a mislabelled
row that only sorts out of the way by luck.
"""
from django.db import migrations
from django.db.models import Q


TRANSLATED = [("FoodCategory", "name"), ("FoodItem", "name"),
              ("Recipe", "name"), ("DietPlanTemplate", "name")]


def fill_english(apps, schema_editor):
    for model_name, field in TRANSLATED:
        Model = apps.get_model("diet", model_name)
        taken = set(Model.objects.exclude(**{f"{field}_en": None})
                    .values_list(f"{field}_en", flat=True))
        for row in Model.objects.filter(Q(**{f"{field}_en": None})
                                        | Q(**{f"{field}_en": ""})).iterator():
            base = getattr(row, field, None)
            # `name` on a historical model is the plain column, not the descriptor, so
            # this reads what was actually written.
            if not base or base in taken:
                continue
            setattr(row, f"{field}_en", base)
            row.save(update_fields=[f"{field}_en"])
            taken.add(base)

    # DietPlanTemplate carries a second translated field.
    Template = apps.get_model("diet", "DietPlanTemplate")
    for row in Template.objects.filter(Q(description_en=None) | Q(description_en="")).iterator():
        if row.description:
            row.description_en = row.description
            row.save(update_fields=["description_en"])


def drop_0005_categories(apps, schema_editor):
    FoodCategory = apps.get_model("diet", "FoodCategory")
    for label in ("Morning Proteins", "Morning Carbs"):
        for row in FoodCategory.objects.filter(Q(name=label) | Q(name_en=label)):
            if not row.fooditem_set.exists():
                row.delete()


def noop(apps, schema_editor):
    """No reverse. Restoring a NULL name_en or a mislabelled category is not a state
    anything wants to be in."""


class Migration(migrations.Migration):

    dependencies = [("diet", "0065_order_by_the_english_name")]

    operations = [
        migrations.RunPython(fill_english, noop),
        migrations.RunPython(drop_0005_categories, noop),
    ]
