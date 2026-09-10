"""What the client says they ate is a real amount, or it is nothing.

`actual_quantity_consumed` took its value straight from a request body: -500 was stored
with a 200, and so was 1,000,000,000. Both fed the client's own progress figures and the
planner's learned weights.

Adding a CHECK to a table that already violates it aborts the migration, so normalise
first — the same order 0069 and 0073 use. A value we cannot believe becomes NULL rather
than being clamped: NULL is what every row that never recorded a separate amount holds,
and it means the planned quantity stands. Clamping would invent a number the client
never gave us.
"""
from django.db import migrations, models


def forget_impossible_amounts(apps, schema_editor):
    MealComponent = apps.get_model("diet", "MealComponent")
    bad = MealComponent.objects.filter(actual_quantity_consumed__isnull=False).exclude(
        actual_quantity_consumed__gte=0, actual_quantity_consumed__lte=1000.0)
    count = bad.count()
    if count:
        bad.update(actual_quantity_consumed=None)
        print(f"  cleared {count} impossible consumed amount(s)")


def noop(apps, schema_editor):
    """No reverse: putting a negative gram figure back is not a state to return to."""


class Migration(migrations.Migration):

    dependencies = [
        ("diet", "0074_a_plan_knows_when_it_was_made"),
    ]

    operations = [
        migrations.RunPython(forget_impossible_amounts, noop),
        migrations.AddConstraint(
            model_name="mealcomponent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("actual_quantity_consumed__isnull", True),
                    models.Q(
                        ("actual_quantity_consumed__gte", 0),
                        ("actual_quantity_consumed__lte", 1000.0),
                    ),
                    _connector="OR",
                ),
                name="consumed_amount_is_a_real_amount",
            ),
        ),
    ]
