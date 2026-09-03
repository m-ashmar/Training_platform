"""Let the database refuse an activity level the calorie calculation cannot weight.

`activity_level` declares `choices`, which DRF enforces on API writes and nothing
enforced anywhere else. Migration 0030 normalised the column once; two rows written
after it ran hold 'moderate'. `calculate_daily_calories` raises on a value outside its
table — deliberately, so a mis-cased row fails loudly rather than being silently
treated as sedentary — and it is called once per row from a serializer method field, so
those two rows returned 500 for the whole of `/api/auth/trainer/client-profile/`.

Normalise again, then constrain, so there is no third time.
"""
from django.db import migrations, models

CHOICES = ["Sedentary", "Light", "Moderate", "Active", "VeryActive"]
ALIASES = {c.lower(): c for c in CHOICES}
ALIASES.update({
    "very active": "VeryActive",
    "very_active": "VeryActive",
    "moderately active": "Moderate",
    "lightly active": "Light",
})


def normalise(apps, schema_editor):
    User = apps.get_model("users", "CustomUser")
    for value in (User.objects.exclude(activity_level__in=CHOICES)
                  .values_list("activity_level", flat=True).distinct()):
        key = (value or "").strip().lower().replace("-", " ")
        User.objects.filter(activity_level=value).update(
            activity_level=ALIASES.get(key, "Sedentary")
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("users", "0030_normalise_activity_level")]
    operations = [
        migrations.RunPython(normalise, noop),
        migrations.AddConstraint(
            model_name="customuser",
            constraint=models.CheckConstraint(
                condition=models.Q(activity_level__in=CHOICES),
                name="user_activity_level_is_a_declared_choice",
            ),
        ),
    ]
