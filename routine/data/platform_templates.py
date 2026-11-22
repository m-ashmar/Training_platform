"""Templates written by the PLATFORM, not by a trainer: `RoutineTemplate` rows with no
author (`created_by` NULL) and a `platform_key`.

`manage.py bootstrap_platform` writes each one ONCE on a new server
(`routine/platform.py ensure_platform_templates`), found again by its key. After that the
row is the operator's: the bootstrap never rewrites a template that exists, so an
exercise swapped in the console stays swapped, and a template deleted in the console
comes back only if the bootstrap runs again. A key is a contract with the database: never
renamed or reused; to add one, append a row with a NEW key.

Exercises are named by `Exercise.catalogue_key` (`routine/data/exercise_catalogue.py`),
the starter catalogue the same command writes first. A template naming an exercise the
database no longer has is not written, and the bootstrap says which.

**Written by an engineer from common practice, for the owner to review** — like the
exercise catalogue, not by a coach. The minutes are the routine's own formula
(`routine/schedule.py day_minutes`: 4 s a rep, the rests between sets, 2 minutes between
exercises), which is what Home and Train show.

THE FREE FIRST WORKOUTS (Road to 10 S2.4, the owner's D10): one for home and one for
the gym, about 20 minutes, needing nothing to carry — every exercise is `bodyweight`.
The gym one uses what any gym floor has (a bench, a low bar); the home one needs only
the floor. Never public: a person gets their own copy (`routine/trial.py`).
"""
from typing import NamedTuple


class Row(NamedTuple):
    exercise: str        # an `Exercise.catalogue_key`
    sets: int
    reps: int
    rest_time: int       # seconds
    day: int = 1


class PlatformTemplate(NamedTuple):
    key: str
    name_en: str
    name_ar: str
    description_en: str
    description_ar: str
    goal: str            # one of `ProductGoal` (`build_muscle`, `strength`, `fat_loss`, `rehab`)
    days: int
    is_public: bool
    rows: tuple
    weeks: int | None = None
    place: str = ''      # `home` | `gym`: where it is meant to be trained


#: `routine/trial.py TRIAL_KEYS` names these two by key. `goal` is required by the model
#: and read by nothing for a trial (it is never listed): a full-body circuit is closest
#: to `fat_loss`. Both come to 20 minutes by the formula.
TRIAL_WORKOUTS = (
    PlatformTemplate(
        key="trial-home",
        name_en="Full body in 20 minutes",
        name_ar="الجسم كاملاً في 20 دقيقة",
        description_en=("Your free first workout: five bodyweight exercises for the whole "
                        "body. All you need is a little floor space."),
        description_ar=("تمرينك الأول المجاني: خمسة تمارين بوزن الجسم لكامل الجسم. "
                        "كل ما تحتاجه مساحة صغيرة على الأرض."),
        goal="fat_loss", days=1, is_public=False, place="home",
        rows=(
            Row("bodyweight-squat", 3, 12, 45),
            Row("push-up", 2, 8, 45),
            Row("glute-bridge", 2, 12, 30),
            Row("superman-hold", 2, 10, 30),
            Row("mountain-climber", 2, 16, 30),
        )),
    PlatformTemplate(
        key="trial-gym",
        name_en="Full body at the gym in 20 minutes",
        name_ar="الجسم كاملاً في النادي خلال 20 دقيقة",
        description_en=("Your free first workout: five bodyweight exercises for the whole "
                        "body, using a bench and a low bar. No weights to choose."),
        description_ar=("تمرينك الأول المجاني: خمسة تمارين بوزن الجسم لكامل الجسم، "
                        "على مقعد وبار منخفض. لا أوزان تحتاج إلى اختيارها."),
        goal="fat_loss", days=1, is_public=False, place="gym",
        rows=(
            Row("bodyweight-squat", 3, 12, 45),
            Row("inverted-row", 2, 8, 45),
            Row("bench-dip", 2, 10, 45),
            Row("glute-bridge", 2, 12, 30),
            Row("russian-twist", 2, 14, 30),
        )),
)

PLATFORM_TEMPLATES = TRIAL_WORKOUTS
