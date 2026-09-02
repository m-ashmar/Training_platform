"""
Backfill `preferred_timezone` for accounts still carrying the frozen default.

The field was declared as `default=getattr(settings, 'TIME_ZONE', 'UTC')`, which
Django evaluates ONCE at import. The column default therefore froze to whatever
TIME_ZONE happened to be the first time the model was loaded — 'UTC' — and stopped
tracking the setting, so 376 of 378 accounts hold 'UTC' while the platform runs on
Asia/Damascus.

This is safe to rewrite because **no user has ever chosen this value**: the field
appeared in no serializer and no endpoint until the reminder work added it, so every
row in the column is a default rather than a preference. Rows already holding
something else are left alone — those are the two that were created after the default
resolved correctly, and any value a user sets from now on.

It matters because `session_reminder` resolves each user's local hour through this
field. Left as-is, 376 users would get their evening reminder on a UTC clock — three
hours early in Damascus, and wrong by more wherever else they are.
"""

from django.conf import settings
from django.db import migrations

# The stale value being corrected. Anything else in the column is deliberate.
FROZEN_DEFAULT = "UTC"


def backfill(apps, schema_editor):
    User = apps.get_model("users", "CustomUser")
    target = getattr(settings, "TIME_ZONE", "UTC")

    if target == FROZEN_DEFAULT:
        # Nothing to correct — the setting and the frozen default agree.
        return

    updated = User.objects.filter(preferred_timezone=FROZEN_DEFAULT).update(
        preferred_timezone=target
    )
    if updated:
        print(f"\n  preferred_timezone: {updated} account(s) moved from UTC to {target}.")


def unbackfill(apps, schema_editor):
    """Deliberately a no-op.

    Reversing would have to set accounts back to 'UTC', which would clobber the choice
    of anyone who has since picked UTC on purpose. There is no way to tell those apart
    from the backfilled rows, so this migration does not pretend it can.
    """
    return


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0028_customuser_workout_reminder_hour_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
