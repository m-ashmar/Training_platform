"""Bring activity_level onto its declared choices.

`calculate_daily_calories` looked the value up in a dict with `.get(value, 1.2)`, so
anything not matching a choice exactly was silently treated as sedentary and the user
was given a target 22.6% below their real maintenance. The lookup now raises instead,
which makes any row still holding an off-choice value fail loudly rather than quietly —
so the rows have to be corrected first.
"""
from django.db import migrations

CHOICES = ["Sedentary", "Light", "Moderate", "Active", "VeryActive"]
# lowercase/spacing variants seen in the data -> the canonical choice
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
        canonical = ALIASES.get(key, "Sedentary")
        User.objects.filter(activity_level=value).update(activity_level=canonical)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("users", "0029_backfill_preferred_timezone")]
    operations = [migrations.RunPython(normalise, noop)]
