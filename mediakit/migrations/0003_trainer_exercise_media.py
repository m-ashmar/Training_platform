"""The per-trainer exercise library: a coach's own demonstration of a shared exercise.

Additive, and nothing is backfilled or rewritten. No row exists with the new owner type
when this runs, so every existing row satisfies every constraint here before and after.

Three constraint moves, and none of them loosens a rule:

  * `one_asset_per_owner_slot` becomes PARTIAL — it no longer covers
    `trainer_exercise`, whose `owner_id` is the exercise and is therefore shared by
    every coach who films it. Unchanged for the five owner types that had it.
  * `one_trainer_asset_per_exercise_slot` is that rule for the new type, with the
    trainer included: one asset per (exercise, trainer, slot).
  * `media_person_is_set_exactly_for_trainers` is replaced by
    `media_person_is_set_exactly_for_people`, the SAME rule over a larger set of owner
    types: `person` is set for exactly the types that belong to a person. Renamed
    because the name stated the old set. This is what the privacy registry leans on —
    it finds a trainer's media through `person` — so widening it, rather than dropping
    it, is the point.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mediakit", "0002_media_video_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="mediaasset",
            name="one_asset_per_owner_slot",
        ),
        migrations.RemoveConstraint(
            model_name="mediaasset",
            name="media_person_is_set_exactly_for_trainers",
        ),
        migrations.AlterField(
            model_name="mediaasset",
            name="owner_type",
            field=models.CharField(
                choices=[
                    ("trainer", "Trainer"),
                    ("exercise", "Exercise"),
                    ("program", "Program"),
                    ("dish", "Dish"),
                    ("app", "App content"),
                    ("trainer_exercise", "Trainer's exercise demo"),
                ],
                db_index=True,
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="mediaasset",
            constraint=models.UniqueConstraint(
                condition=models.Q(("owner_type", "trainer_exercise"), _negated=True),
                fields=("owner_type", "owner_id", "slot"),
                name="one_asset_per_owner_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="mediaasset",
            constraint=models.UniqueConstraint(
                condition=models.Q(("owner_type", "trainer_exercise")),
                fields=("owner_type", "owner_id", "person", "slot"),
                name="one_trainer_asset_per_exercise_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="mediaasset",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("owner_type__in", ["trainer", "trainer_exercise"]),
                        ("person__isnull", False),
                    ),
                    models.Q(
                        models.Q(
                            ("owner_type__in", ["trainer", "trainer_exercise"]),
                            _negated=True,
                        ),
                        ("person__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="media_person_is_set_exactly_for_people",
            ),
        ),
    ]
