"""Copy each plan's existing text into its English column.

modeltranslation reads `name_<active language>` and falls back through
`MODELTRANSLATION_FALLBACK_LANGUAGES`. A row whose `name_en` is empty reads as empty
through the API even though the plain `name` column is populated — the same failure
that once returned blank names for 542 of 554 exercises. Adding the columns without
this leaves every plan nameless.
"""
from django.db import migrations


def seed_english(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscription", "SubscriptionPlan")
    for plan in SubscriptionPlan.objects.all().iterator():
        SubscriptionPlan.objects.filter(pk=plan.pk).update(
            name_en=plan.name_en or plan.name,
            description_en=plan.description_en or plan.description,
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("subscription", "0009_subscriptionplan_description_ar_and_more")]
    operations = [migrations.RunPython(seed_english, noop)]
