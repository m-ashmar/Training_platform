"""Give every meal a time, then make the uniqueness key real.

`unique_together = (diet_plan, date, meal_type, scheduled_time)` has been on this model
for a long time and has never once fired: `scheduled_time` is nullable, Postgres treats
NULLs as distinct, and nothing wrote a time. Measured on the development database before
this ran: 2,569 of 2,720 meals had no scheduled time and 109 (plan, date, meal_type)
groups held more than one row. A client with two identical breakfasts can never reach 100%
for that day.

`nulls_distinct=False` closes it, but adding a unique index to a table that already holds
duplicates aborts the migration mid-deploy — the lesson 0054 did not learn and 0064 did.
So the rows are separated first, and separated rather than deleted: these are meals people
were served. Each one gets the time its slot is actually eaten at, and a second meal in the
same slot on the same day is offset, exactly as `meal_plan_factory.scheduled_time_for`
does at generation. That is also the answer to "what is breakfast, what is lunch": a slot
now carries a time the client can read.
"""
from datetime import date as date_cls, datetime, time, timedelta

from django.db import migrations, models

# Kept in step with diet/services/meal_plan_factory.py. Copied rather than imported:
# a migration must keep doing what it did when it ran, not what today's code says.
SLOT_TIMES = {
    "Breakfast": time(8, 0),
    "Lunch": time(13, 30),
    "Dinner": time(20, 0),
    "Snack": time(11, 0),
}
REPEAT_OFFSET = timedelta(hours=5)


def time_for(slot, occurrence):
    base = SLOT_TIMES.get(slot, SLOT_TIMES["Lunch"])
    if not occurrence:
        return base
    moved = datetime.combine(date_cls.min, base) + REPEAT_OFFSET * occurrence
    if moved.date() != date_cls.min:
        return time(23, max(0, 59 - occurrence))
    return moved.time()


def give_every_meal_a_time(apps, schema_editor):
    Meal = apps.get_model("diet", "Meal")
    seen = set()
    # Rows that already carry a time keep it and claim their slot first, so a backfilled
    # row is never given a time that is already taken.
    for key in (Meal.objects.exclude(scheduled_time=None)
                .values_list("diet_plan_id", "date", "meal_type", "scheduled_time")):
        seen.add(key)

    updates = []
    for meal in (Meal.objects.filter(scheduled_time=None)
                 .order_by("diet_plan_id", "date", "meal_type", "id").iterator()):
        occurrence = 0
        while True:
            candidate = time_for(meal.meal_type, occurrence)
            key = (meal.diet_plan_id, meal.date, meal.meal_type, candidate)
            if key not in seen:
                break
            occurrence += 1
            if occurrence > 60:
                # 60 distinct times in one slot on one day is not real data. Leave the
                # row alone rather than invent a time; the constraint below will name it.
                candidate = None
                break
        if candidate is None:
            continue
        seen.add(key)
        meal.scheduled_time = candidate
        updates.append(meal)
        if len(updates) >= 500:
            Meal.objects.bulk_update(updates, ["scheduled_time"])
            updates = []
    if updates:
        Meal.objects.bulk_update(updates, ["scheduled_time"])


def noop(apps, schema_editor):
    """No reverse. Taking the times back off would restore the duplicates."""


class Migration(migrations.Migration):

    dependencies = [
        ("diet", "0066_english_name_on_every_translated_row"),
    ]

    operations = [
        migrations.AlterUniqueTogether(name="meal", unique_together=set()),
        migrations.RunPython(give_every_meal_a_time, noop),
        migrations.AddConstraint(
            model_name="meal",
            constraint=models.UniqueConstraint(
                fields=("diet_plan", "date", "meal_type", "scheduled_time"),
                name="one_meal_per_slot_per_day",
                nulls_distinct=False,
            ),
        ),
    ]
