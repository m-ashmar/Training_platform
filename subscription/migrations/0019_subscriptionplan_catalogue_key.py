"""Give each default plan a key the console cannot change (SIDE_BUGS S-236).

`bootstrap_platform` found a default plan by its English name. After an operator renamed
one, the next run wrote a second ACTIVE plan at the placeholder price, or died on the
unique Arabic name before its later steps. The launch commands now find a default plan
by `catalogue_key`.

The backfill keys the plan that still carries a default English name, the oldest one if
there are several. A plan an operator already renamed stays unkeyed: nothing here can
tell which row it was, and `ensure_default_plans` never re-creates a missing default on a
database that has plans, so it is reported, not duplicated.

The names are copied, not imported: a migration is history.
"""
from django.db import migrations, models

DEFAULT_KEYS = {
    "free": "Free Plan",
    "basic": "Basic Plan",
    "premium": "Premium Plan",
    "professional": "Professional Plan",
}


def key_the_default_plans(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscription", "SubscriptionPlan")
    for key, name in DEFAULT_KEYS.items():
        if SubscriptionPlan.objects.filter(catalogue_key=key).exists():
            continue
        plan = (SubscriptionPlan.objects.filter(name_en=name, catalogue_key__isnull=True)
                .order_by("id").first())
        if plan is not None:
            SubscriptionPlan.objects.filter(pk=plan.pk).update(catalogue_key=key)


class Migration(migrations.Migration):

    dependencies = [
        ("subscription", "0018_payer_address_and_gateway_health"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionplan",
            name="catalogue_key",
            field=models.CharField(
                blank=True, editable=False, max_length=32, null=True, unique=True
            ),
        ),
        migrations.RunPython(key_the_default_plans, migrations.RunPython.noop),
    ]
