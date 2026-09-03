"""Make the usage window a computed boundary, collapse the rows that proves were duplicates.

`period_start` was `auto_now_add=True`, so every insert got its own microsecond and the
`(subscription, feature, period_start)` unique key could never be violated. Concurrent
requests each inserted a row; from the second one on, the permission check's
`get_or_create(subscription, feature)` raised `MultipleObjectsReturned`, a bare
`except:` turned that into a denial, and the paying subscriber lost access to diet and
meal generation permanently and silently.

Eight parallel requests produced eight rows in a probe. This collapses whatever is
there — earliest row wins, counts are summed so nothing already spent is given back —
and pins each survivor's `period_start` to the boundary `subscription.quota` computes,
so the constraint has something real to hold.

It also seeds the two declared features. `SubscriptionUsageLimit` used to create them
from a read path, which meant a typo in a permission declaration silently populated the
table.
"""
from django.db import migrations, models
from datetime import timedelta

FEATURES = {
    "daily_meals": "Meals and diet plans a subscriber may generate per day.",
    "routines": "Routines a subscriber may hold in a paid period.",
}
GRANULARITY = {"daily_meals": "day", "routines": "subscription"}


def period_start_for(subscription, granularity, when):
    if granularity == "day":
        return when.replace(hour=0, minute=0, second=0, microsecond=0)
    return subscription.start_date or when


def repair(apps, schema_editor):
    SubscriptionFeature = apps.get_model("subscription", "SubscriptionFeature")
    SubscriptionUsage = apps.get_model("subscription", "SubscriptionUsage")

    for name, description in FEATURES.items():
        SubscriptionFeature.objects.get_or_create(
            name=name, defaults={"description": description, "is_active": True}
        )

    seen = {}
    for usage in SubscriptionUsage.objects.select_related("subscription").order_by("period_start", "id"):
        gran = GRANULARITY.get(usage.feature.name if hasattr(usage, "feature") else "", "subscription")
        try:
            start = period_start_for(usage.subscription, gran, usage.period_start)
        except Exception:
            start = usage.period_start
        key = (usage.subscription_id, usage.feature_id, start)
        if key in seen:
            keeper = seen[key]
            keeper.usage_count += usage.usage_count
            keeper.save(update_fields=["usage_count"])
            usage.delete()
            continue
        usage.period_start = start
        if not usage.period_end or usage.period_end <= start:
            usage.period_end = start + timedelta(days=30)
        usage.save(update_fields=["period_start", "period_end"])
        seen[key] = usage


def noop(apps, schema_editor):
    """Not reversible: the duplicate rows were the fault, not information."""


class Migration(migrations.Migration):
    dependencies = [("subscription", "0007_plan_currency_is_the_single_source")]
    operations = [
        migrations.AlterField(
            model_name="subscriptionusage",
            name="period_start",
            field=models.DateTimeField(
                db_index=True,
                help_text=(
                    "Start of the window this row counts. Computed by subscription.quota, "
                    "not stamped on insert: it was auto_now_add, so every row got a "
                    "distinct value and the unique key below could never be violated. "
                    "Concurrent requests each inserted their own row and the lookup then "
                    "raised MultipleObjectsReturned."
                ),
            ),
        ),
        migrations.RunPython(repair, noop),
    ]
