"""Leave one active diet plan per user per stretch of dates.

`DietPlan.clean()` has always said a user may not hold two active plans covering the
same days, and nothing ran it. Generation did not supersede the previous plan either,
so taps accumulated: 274 of 333 plans here are active, 18 users hold more than one,
and one holds 69. For any shared day it was undefined which plan applied, and `Meal`'s
uniqueness is scoped to the plan rather than the user, so each of them could write its
own breakfast and the client saw every one.

Generation now supersedes overlaps inside a locked transaction. This clears what
accumulated before it did: newest plan wins, everything it overlaps is deactivated.
Nothing is deleted — the meals and the history stay readable.
"""
from django.db import migrations


def retire_overlaps(apps, schema_editor):
    DietPlan = apps.get_model("diet", "DietPlan")

    retired = 0
    user_ids = DietPlan.objects.filter(is_active=True).values_list("user_id", flat=True).distinct()
    for user_id in list(user_ids):
        kept = []
        plans = list(
            DietPlan.objects.filter(user_id=user_id, is_active=True)
            .order_by("-start_date", "-id")
        )
        for plan in plans:
            overlaps = any(
                plan.start_date <= k.end_date and plan.end_date >= k.start_date
                for k in kept
            )
            if overlaps:
                plan.is_active = False
                retired += 1
            else:
                kept.append(plan)
        DietPlan.objects.filter(
            id__in=[p.id for p in plans if not p.is_active]
        ).update(is_active=False)
    print(f"    retired {retired} overlapping active diet plan(s)")


def noop(apps, schema_editor):
    """Not reversible: which plan was active is exactly what was ambiguous."""


class Migration(migrations.Migration):
    dependencies = [("diet", "0048_fooditem_needs_review_and_catalogue_repair")]
    operations = [migrations.RunPython(retire_overlaps, noop)]
