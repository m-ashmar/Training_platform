from django.db import migrations


def swap_inverted_dates(apps, schema_editor):
    """Repair plans whose end_date precedes start_date.

    Four rows in the dev catalogue had the two swapped. The check constraint added in
    the next migration cannot be created while they exist, and an inverted range is
    meaningless anyway — a plan cannot end before it begins.
    """
    DietPlan = apps.get_model('diet', 'DietPlan')
    inverted = DietPlan.objects.filter(end_date__lt=models.F('start_date'))
    for plan in inverted:
        plan.start_date, plan.end_date = plan.end_date, plan.start_date
        plan.save(update_fields=['start_date', 'end_date'])


def noop(apps, schema_editor):
    pass


from django.db import models  # noqa: E402  (used inside the data function)


class Migration(migrations.Migration):

    dependencies = [
        ('diet', '0041_alter_dailyprogress_options_alter_dietplan_options_and_more'),
    ]

    operations = [
        migrations.RunPython(swap_inverted_dates, noop),
    ]
