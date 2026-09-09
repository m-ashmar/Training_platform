"""One row per food per meal — after collapsing the rows that already break it.

The constraint was added on its own. Adding a unique index to a table that already holds
duplicates aborts the migration, and this table is written by a resolver that could put
the same food on a plate twice, so any database with real history had them. The deploy
would have stopped here, half-migrated.

Duplicates are merged, not deleted: two rows for the same food in one meal are one
portion split in two, so the grams are summed onto the oldest row and the extras removed.
Whatever the client has eaten is preserved — `is_completed` and `actual_quantity_consumed`
carry over if any of the merged rows recorded them.
"""
from django.db import migrations, models
from django.db.models import Count


def collapse_duplicates(apps, schema_editor):
    MealComponent = apps.get_model("diet", "MealComponent")
    groups = (MealComponent.objects.values("meal_id", "food_id")
              .annotate(n=Count("id")).filter(n__gt=1).order_by())
    for group in groups:
        rows = list(MealComponent.objects
                    .filter(meal_id=group["meal_id"], food_id=group["food_id"])
                    .order_by("id"))
        keeper, extras = rows[0], rows[1:]
        keeper.quantity = sum(float(r.quantity or 0.0) for r in rows)
        if any(r.is_completed for r in rows):
            keeper.is_completed = True
            keeper.completed_at = next(
                (r.completed_at for r in rows if r.completed_at), keeper.completed_at)
            eaten = [float(r.actual_quantity_consumed) for r in rows
                     if r.actual_quantity_consumed is not None]
            if eaten:
                keeper.actual_quantity_consumed = sum(eaten)
        keeper.save()
        MealComponent.objects.filter(id__in=[r.id for r in extras]).delete()


def noop(apps, schema_editor):
    """No reverse. Splitting a merged portion back in two is not recoverable."""


class Migration(migrations.Migration):

    dependencies = [
        ("diet", "0053_fooditem_meal_slots"),
    ]

    operations = [
        migrations.RunPython(collapse_duplicates, noop),
        migrations.AddConstraint(
            model_name="mealcomponent",
            constraint=models.UniqueConstraint(
                fields=("meal", "food"), name="one_row_per_food_per_meal"
            ),
        ),
    ]
