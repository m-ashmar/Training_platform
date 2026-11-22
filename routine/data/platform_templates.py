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

THE STARTER TEMPLATES (Road to 10 S2.10, the owner's D16): eight public templates, one
per goal of the closed list and per place, for a trainer to copy and adapt. Below.
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


def _week(*days):
    """Rows for a template of len(days) training days: each day a tuple of
    (catalogue key, sets, reps, rest seconds)."""
    return tuple(Row(key, sets, reps, rest, day=number)
                 for number, rows in enumerate(days, start=1)
                 for key, sets, reps, rest in rows)


#: THE STARTER TEMPLATES (Road to 10 S2.10, the owner's D16: "written by the platform
#: only, at launch"). Public, so every trainer finds them in `templates/public_templates/`
#: (`?by_platform=true` for these alone) and copies one into their own library
#: (`templates/{id}/copy/`) to adapt — «ابدأ من قالب جاهز». One per goal and place: the
#: four goals of the closed list, each for home (bodyweight and dumbbells) and for the
#: gym. Three training days a week; `weeks` is how long the program is meant to run, for
#: the trainer to change. The rehab two are light, general starting points for a trainer
#: to fit to the client's injury — not a treatment.
STARTER_TEMPLATES = (
    PlatformTemplate(
        key="starter-build-muscle-gym",
        name_en="Build muscle · gym · push, pull, legs",
        name_ar="بناء العضلات · النادي · دفع، سحب، أرجل",
        description_en=("Three days a week: a push day (chest, shoulders, triceps), a pull "
                        "day (back, biceps) and a leg day. Moderate reps, steady rests."),
        description_ar=("ثلاثة أيام في الأسبوع: يوم دفع (صدر وأكتاف وترايسبس)، ويوم سحب "
                        "(ظهر وبايسبس)، ويوم أرجل. تكرارات متوسطة وراحة ثابتة."),
        goal="build_muscle", place="gym", days=3, weeks=8, is_public=True,
        rows=_week(
            (("barbell-bench-press", 4, 8, 90), ("incline-dumbbell-press", 3, 10, 75),
             ("seated-dumbbell-shoulder-press", 3, 10, 75),
             ("dumbbell-lateral-raise", 3, 12, 60), ("triceps-rope-pushdown", 3, 12, 60)),
            (("lat-pulldown", 4, 10, 90), ("seated-cable-row", 3, 10, 75),
             ("one-arm-dumbbell-row", 3, 10, 75), ("face-pull", 3, 15, 60),
             ("barbell-curl", 3, 10, 60)),
            (("barbell-back-squat", 4, 8, 120), ("barbell-romanian-deadlift", 3, 10, 90),
             ("leg-press", 3, 12, 90), ("lying-leg-curl", 3, 12, 60),
             ("standing-calf-raise", 3, 15, 60)),
        )),
    PlatformTemplate(
        key="starter-build-muscle-home",
        name_en="Build muscle · home · dumbbells",
        name_ar="بناء العضلات · المنزل · دمبل",
        description_en=("Three days a week with a pair of dumbbells: an upper-body day, a "
                        "lower-body day and a full-body day."),
        description_ar=("ثلاثة أيام في الأسبوع بزوج من الدمبل: يوم للجزء العلوي، ويوم "
                        "للجزء السفلي، ويوم لكامل الجسم."),
        goal="build_muscle", place="home", days=3, weeks=8, is_public=True,
        rows=_week(
            (("dumbbell-bench-press", 4, 10, 75), ("one-arm-dumbbell-row", 4, 10, 75),
             ("seated-dumbbell-shoulder-press", 3, 10, 75), ("dumbbell-curl", 3, 12, 60),
             ("overhead-dumbbell-triceps-extension", 3, 12, 60)),
            (("goblet-squat", 4, 12, 75), ("dumbbell-romanian-deadlift", 3, 10, 75),
             ("bulgarian-split-squat", 3, 10, 75), ("glute-bridge", 3, 15, 60),
             ("single-leg-calf-raise", 3, 15, 45)),
            (("push-up", 3, 12, 60), ("dumbbell-walking-lunge", 3, 10, 75),
             ("incline-dumbbell-press", 3, 10, 75), ("dumbbell-lateral-raise", 3, 12, 60),
             ("crunch", 3, 15, 45)),
        )),
    PlatformTemplate(
        key="starter-strength-gym",
        name_en="Strength · gym · 5×5",
        name_ar="القوة · النادي · 5×5",
        description_en=("Three days a week on the big barbell lifts: five sets of five, "
                        "long rests, adding weight when every set is done."),
        description_ar=("ثلاثة أيام في الأسبوع على رفعات البار الأساسية: خمس مجموعات من "
                        "خمس تكرارات، وراحة طويلة، وزيادة الوزن حين تكتمل كل المجموعات."),
        goal="strength", place="gym", days=3, weeks=8, is_public=True,
        rows=_week(
            (("barbell-back-squat", 5, 5, 180), ("barbell-bench-press", 5, 5, 180),
             ("barbell-bent-over-row", 3, 6, 120)),
            (("deadlift", 3, 5, 180), ("barbell-overhead-press", 5, 5, 150),
             ("pull-up", 3, 6, 120)),
            (("barbell-back-squat", 5, 5, 180), ("close-grip-bench-press", 3, 6, 120),
             ("barbell-hip-thrust", 3, 8, 120)),
        )),
    PlatformTemplate(
        key="starter-strength-home",
        name_en="Strength · home · dumbbells",
        name_ar="القوة · المنزل · دمبل",
        description_en=("Three days a week with heavy dumbbells and hard bodyweight moves: "
                        "few reps, long rests."),
        description_ar=("ثلاثة أيام في الأسبوع بدمبل ثقيل وتمارين صعبة بوزن الجسم: "
                        "تكرارات قليلة وراحة طويلة."),
        goal="strength", place="home", days=3, weeks=8, is_public=True,
        rows=_week(
            (("goblet-squat", 5, 6, 120), ("dumbbell-bench-press", 5, 6, 120),
             ("one-arm-dumbbell-row", 4, 6, 90)),
            (("dumbbell-romanian-deadlift", 4, 6, 120),
             ("seated-dumbbell-shoulder-press", 4, 6, 120), ("diamond-push-up", 3, 8, 90)),
            (("bulgarian-split-squat", 4, 6, 120), ("pike-push-up", 4, 6, 90),
             ("inverted-row", 4, 8, 90)),
        )),
    PlatformTemplate(
        key="starter-fat-loss-gym",
        name_en="Fat loss · gym · circuits",
        name_ar="خسارة الدهون · النادي · دوائر",
        description_en=("Three days a week of whole-body circuits on the machines: higher "
                        "reps and short rests to keep the heart rate up."),
        description_ar=("ثلاثة أيام في الأسبوع من دوائر لكامل الجسم على الآلات: تكرارات "
                        "أعلى وراحة قصيرة لإبقاء النبض مرتفعاً."),
        goal="fat_loss", place="gym", days=3, weeks=8, is_public=True,
        rows=_week(
            (("leg-press", 3, 15, 45), ("machine-chest-press", 3, 15, 45),
             ("seated-cable-row", 3, 15, 45), ("mountain-climber", 3, 20, 30),
             ("russian-twist", 3, 20, 30)),
            (("goblet-squat", 3, 15, 45), ("lat-pulldown", 3, 15, 45),
             ("machine-shoulder-press", 3, 12, 45), ("dumbbell-walking-lunge", 3, 12, 45),
             ("crunch", 3, 20, 30)),
            (("barbell-romanian-deadlift", 3, 12, 60), ("dumbbell-bench-press", 3, 12, 45),
             ("machine-row", 3, 15, 45), ("leg-extension", 3, 15, 45),
             ("mountain-climber", 3, 20, 30)),
        )),
    PlatformTemplate(
        key="starter-fat-loss-home",
        name_en="Fat loss · home · bodyweight",
        name_ar="خسارة الدهون · المنزل · وزن الجسم",
        description_en=("Three days a week of bodyweight circuits, with one pair of "
                        "dumbbells on the second day: short rests, many reps."),
        description_ar=("ثلاثة أيام في الأسبوع من دوائر بوزن الجسم، مع زوج من الدمبل في "
                        "اليوم الثاني: راحة قصيرة وتكرارات كثيرة."),
        goal="fat_loss", place="home", days=3, weeks=8, is_public=True,
        rows=_week(
            (("bodyweight-squat", 4, 15, 30), ("push-up", 4, 10, 30),
             ("mountain-climber", 4, 20, 30), ("glute-bridge", 3, 15, 30),
             ("crunch", 3, 20, 30)),
            (("goblet-squat", 4, 12, 30), ("inverted-row", 3, 10, 45),
             ("bench-dip", 3, 12, 45), ("russian-twist", 3, 20, 30),
             ("mountain-climber", 4, 20, 30)),
            (("bulgarian-split-squat", 3, 10, 45), ("push-up", 3, 12, 30),
             ("superman-hold", 3, 12, 30), ("single-leg-calf-raise", 3, 15, 30),
             ("crunch", 3, 20, 30)),
        )),
    PlatformTemplate(
        key="starter-rehab-gym",
        name_en="Rehab · gym · light machines",
        name_ar="إعادة التأهيل · النادي · آلات بأوزان خفيفة",
        description_en=("A gentle return to training on the machines: two sets, light "
                        "loads, slow and controlled. A starting point for the trainer to fit "
                        "to the client's injury, not a treatment."),
        description_ar=("عودة هادئة إلى التمرين على الآلات: مجموعتان بأوزان خفيفة وحركة "
                        "بطيئة ومضبوطة. نقطة بداية يكيّفها المدرب مع إصابة المتدرّب، وليست "
                        "علاجاً."),
        goal="rehab", place="gym", days=3, weeks=6, is_public=True,
        rows=_week(
            (("leg-press", 2, 12, 90), ("leg-extension", 2, 15, 60),
             ("lying-leg-curl", 2, 15, 60), ("hip-abduction-machine", 2, 15, 60),
             ("glute-bridge", 2, 12, 60)),
            (("seated-cable-row", 2, 12, 90), ("face-pull", 2, 15, 60),
             ("reverse-pec-deck", 2, 15, 60), ("machine-chest-press", 2, 12, 90),
             ("cable-lateral-raise", 2, 12, 60)),
            (("goblet-squat", 2, 10, 90), ("lat-pulldown", 2, 12, 90),
             ("hip-adduction-machine", 2, 15, 60), ("standing-calf-raise", 2, 15, 60),
             ("superman-hold", 2, 10, 60)),
        )),
    PlatformTemplate(
        key="starter-rehab-home",
        name_en="Rehab · home · gentle",
        name_ar="إعادة التأهيل · المنزل · تمارين لطيفة",
        description_en=("A gentle return to training at home: two sets, bodyweight or light "
                        "dumbbells, slow and pain-free. A starting point for the trainer to "
                        "fit to the client's injury, not a treatment."),
        description_ar=("عودة هادئة إلى التمرين في المنزل: مجموعتان بوزن الجسم أو بدمبل "
                        "خفيف، بحركة بطيئة وبلا ألم. نقطة بداية يكيّفها المدرب مع إصابة "
                        "المتدرّب، وليست علاجاً."),
        goal="rehab", place="home", days=3, weeks=6, is_public=True,
        rows=_week(
            (("glute-bridge", 2, 12, 60), ("bodyweight-squat", 2, 10, 60),
             ("single-leg-calf-raise", 2, 12, 60), ("superman-hold", 2, 10, 60)),
            (("inverted-row", 2, 10, 60), ("bent-over-rear-delt-fly", 2, 12, 60),
             ("dumbbell-lateral-raise", 2, 12, 60), ("dumbbell-curl", 2, 12, 60)),
            (("glute-bridge", 2, 15, 60), ("goblet-squat", 2, 10, 75),
             ("dumbbell-romanian-deadlift", 2, 10, 75), ("crunch", 2, 12, 60)),
        )),
)

PLATFORM_TEMPLATES = TRIAL_WORKOUTS + STARTER_TEMPLATES
