"""Remove rows that are test fixtures wearing a food's clothes.

Eighteen rows named "Manual Food 0" through "Manual Food 19" sit in a category called
`Protein_Manual` with 20 g of protein each, which is enough to rank them second through
fifth in every protein slot the planner offers. They are seed data from an old probe and
a client could be served one.

Deleting is safe: `MealComponent.food` is CASCADE, so any historical meal referencing
one goes with it, and a meal built from a fixture is not a record worth keeping. The
development database is being dropped before launch in any case; this exists so the same
rows cannot reappear in a fresh one.
"""
from django.db import migrations


def purge(apps, schema_editor):
    FoodItem = apps.get_model("diet", "FoodItem")
    FoodCategory = apps.get_model("diet", "FoodCategory")

    removed = FoodItem.objects.filter(name__regex=r"^Manual Food \d+$").delete()[0]
    removed += FoodItem.objects.filter(name__iexact="test").delete()[0]
    FoodCategory.objects.filter(name="Protein_Manual", fooditem__isnull=True).delete()
    if removed:
        print(f"    removed {removed} test row(s) from the food catalogue")


def noop(apps, schema_editor):
    """Not reversible: these were never real foods."""


class Migration(migrations.Migration):
    dependencies = [("diet", "0051_fooditem_household_unit_fooditem_max_units_and_more")]
    operations = [migrations.RunPython(purge, noop)]
