"""The STARTER exercise catalogue: the platform's shared exercises on a new database.

**A starting point for the owner to review and extend, not a finished library.** It was
written by an engineer from common gym practice, not by a coach, and nobody has checked
the Arabic names with the trainers who will read them. `manage.py bootstrap_platform`
writes each row ONCE, as a global exercise (no owner, visible to every trainer), with
`Exercise.catalogue_key` set to the key below — that column is how a row is known to
have come from here, so the console can list exactly these for review
(`routine/admin.py`, "Starter catalogue" filter). After that the database row is the
owner's: the bootstrap never rewrites a row that exists, so a name corrected in the
console stays corrected, and a row deleted in the console comes back only if the
bootstrap runs again.

To add an exercise: append a row with a NEW key. A key is a contract with the database
(it finds the row again), so an existing key is never renamed or reused; to retire an
exercise, deactivate it in the console.

Columns:

    key            stable slug, unique; never renamed
    name_en        English name
    name_ar        Arabic name (the app is Arabic-first)
    target_muscle  a stored key of `Exercise.target_muscle` (its `choices`)
    equipment      a code of `routine.models.EQUIPMENT_CHOICES`
    difficulty     a key of `Exercise.difficulty_level`
    kind           "compound" (several joints) or "isolation" (one) — for review only
    home           True when it needs nothing a home does not have (the body, dumbbells)
                   — for review only

`kind` and `home` are not stored: the model has no column for them. They are here so
the reviewer can see the mix at a glance and so `tests/test_bootstrap_commands.py` can
hold the catalogue to "compound and isolation, gym and home".
"""
from typing import NamedTuple


class StarterExercise(NamedTuple):
    key: str
    name_en: str
    name_ar: str
    target_muscle: str
    equipment: str
    difficulty: str
    kind: str
    home: bool


C, I = "compound", "isolation"

STARTER_EXERCISES = tuple(StarterExercise(*row) for row in (
    # ------------------------------------------------------------------ chest
    ("barbell-bench-press", "Barbell Bench Press", "ضغط الصدر بالبار",
     "Middle Chest", "barbell", "intermediate", C, False),
    ("incline-barbell-bench-press", "Incline Barbell Bench Press", "ضغط الصدر المائل بالبار",
     "Upper Chest", "barbell", "intermediate", C, False),
    ("dumbbell-bench-press", "Dumbbell Bench Press", "ضغط الصدر بالدمبل",
     "Middle Chest", "dumbbell", "beginner", C, True),
    ("incline-dumbbell-press", "Incline Dumbbell Press", "ضغط الصدر المائل بالدمبل",
     "Upper Chest", "dumbbell", "beginner", C, True),
    ("dumbbell-fly", "Dumbbell Fly", "تفتيح الصدر بالدمبل",
     "Middle Chest", "dumbbell", "beginner", I, True),
    ("cable-crossover", "Cable Crossover", "تجميع الصدر بالكابل",
     "Lower Chest", "cable", "intermediate", I, False),
    ("machine-chest-press", "Machine Chest Press", "ضغط الصدر على الآلة",
     "Middle Chest", "machine", "beginner", C, False),
    ("pec-deck-fly", "Pec Deck Fly", "تفتيح الصدر على الآلة",
     "Middle Chest", "machine", "beginner", I, False),
    ("push-up", "Push-Up", "تمرين الضغط",
     "Middle Chest", "bodyweight", "beginner", C, True),
    ("chest-dip", "Chest Dip", "المتوازي للصدر",
     "Lower Chest", "bodyweight", "intermediate", C, False),
    # ------------------------------------------------------------------- back
    ("pull-up", "Pull-Up", "العقلة",
     "Lats", "bodyweight", "intermediate", C, False),
    ("lat-pulldown", "Lat Pulldown", "السحب العلوي بالكابل",
     "Lats", "cable", "beginner", C, False),
    ("seated-cable-row", "Seated Cable Row", "التجديف جالساً بالكابل",
     "Upper Back", "cable", "beginner", C, False),
    ("barbell-bent-over-row", "Barbell Bent-Over Row", "التجديف بالبار منحنياً",
     "Upper Back", "barbell", "intermediate", C, False),
    ("one-arm-dumbbell-row", "One-Arm Dumbbell Row", "التجديف بالدمبل بذراع واحدة",
     "Lats", "dumbbell", "beginner", C, True),
    ("machine-row", "Machine Row", "التجديف على الآلة",
     "Upper Back", "machine", "beginner", C, False),
    ("inverted-row", "Inverted Row", "التجديف المقلوب",
     "Upper Back", "bodyweight", "beginner", C, True),
    ("deadlift", "Deadlift", "الرفعة الميتة",
     "Lower Back", "barbell", "advanced", C, False),
    ("superman-hold", "Superman Hold", "تمرين سوبرمان",
     "Lower Back", "bodyweight", "beginner", I, True),
    ("barbell-shrug", "Barbell Shrug", "رفع الكتفين بالبار",
     "Traps", "barbell", "beginner", I, False),
    ("dumbbell-shrug", "Dumbbell Shrug", "رفع الكتفين بالدمبل",
     "Traps", "dumbbell", "beginner", I, True),
    # -------------------------------------------------------------- shoulders
    ("barbell-overhead-press", "Standing Barbell Overhead Press", "ضغط الأكتاف بالبار واقفاً",
     "Front Deltoid", "barbell", "intermediate", C, False),
    ("seated-dumbbell-shoulder-press", "Seated Dumbbell Shoulder Press",
     "ضغط الأكتاف بالدمبل جالساً", "Front Deltoid", "dumbbell", "beginner", C, True),
    ("arnold-press", "Arnold Press", "ضغط أرنولد بالدمبل",
     "Front Deltoid", "dumbbell", "intermediate", C, True),
    ("machine-shoulder-press", "Machine Shoulder Press", "ضغط الأكتاف على الآلة",
     "Front Deltoid", "machine", "beginner", C, False),
    ("pike-push-up", "Pike Push-Up", "الضغط الهرمي للأكتاف",
     "Front Deltoid", "bodyweight", "intermediate", C, True),
    ("dumbbell-lateral-raise", "Dumbbell Lateral Raise", "الرفرفة الجانبية بالدمبل",
     "Lateral Deltoid", "dumbbell", "beginner", I, True),
    ("cable-lateral-raise", "Cable Lateral Raise", "الرفرفة الجانبية بالكابل",
     "Lateral Deltoid", "cable", "intermediate", I, False),
    ("dumbbell-front-raise", "Dumbbell Front Raise", "الرفرفة الأمامية بالدمبل",
     "Front Deltoid", "dumbbell", "beginner", I, True),
    ("bent-over-rear-delt-fly", "Bent-Over Rear Delt Fly", "الرفرفة الخلفية بالدمبل منحنياً",
     "Rear Deltoid", "dumbbell", "beginner", I, True),
    ("face-pull", "Face Pull", "السحب نحو الوجه بالكابل",
     "Rear Deltoid", "cable", "intermediate", I, False),
    ("reverse-pec-deck", "Reverse Pec Deck", "الرفرفة الخلفية على الآلة",
     "Rear Deltoid", "machine", "beginner", I, False),
    # ------------------------------------------------------------------- arms
    ("barbell-curl", "Barbell Curl", "ثني الذراعين بالبار",
     "Biceps", "barbell", "beginner", I, False),
    ("dumbbell-curl", "Dumbbell Curl", "ثني الذراعين بالدمبل",
     "Biceps", "dumbbell", "beginner", I, True),
    ("hammer-curl", "Hammer Curl", "ثني المطرقة بالدمبل",
     "Biceps", "dumbbell", "beginner", I, True),
    ("cable-curl", "Cable Curl", "ثني الذراعين بالكابل",
     "Biceps", "cable", "beginner", I, False),
    ("triceps-rope-pushdown", "Triceps Rope Pushdown", "دفع الترايسبس للأسفل بالحبل",
     "Triceps", "cable", "beginner", I, False),
    ("lying-barbell-triceps-extension", "Lying Barbell Triceps Extension",
     "تمديد الترايسبس بالبار مستلقياً", "Triceps", "barbell", "intermediate", I, False),
    ("overhead-dumbbell-triceps-extension", "Overhead Dumbbell Triceps Extension",
     "تمديد الترايسبس بالدمبل فوق الرأس", "Triceps", "dumbbell", "beginner", I, True),
    ("close-grip-bench-press", "Close-Grip Bench Press", "ضغط البار بقبضة ضيقة",
     "Triceps", "barbell", "intermediate", C, False),
    ("bench-dip", "Bench Dip", "الغطس على المقعد",
     "Triceps", "bodyweight", "beginner", C, True),
    ("diamond-push-up", "Diamond Push-Up", "الضغط الماسي",
     "Triceps", "bodyweight", "intermediate", C, True),
    ("dumbbell-wrist-curl", "Dumbbell Wrist Curl", "ثني الرسغ بالدمبل",
     "Forearms", "dumbbell", "beginner", I, True),
    ("farmers-walk", "Farmer's Walk", "مشية المزارع",
     "Forearms", "dumbbell", "beginner", C, True),
    # ------------------------------------------------------------------- legs
    ("barbell-back-squat", "Barbell Back Squat", "السكوات الخلفي بالبار",
     "Front Quads", "barbell", "intermediate", C, False),
    ("goblet-squat", "Goblet Squat", "سكوات الكأس بالدمبل",
     "Front Quads", "dumbbell", "beginner", C, True),
    ("bodyweight-squat", "Bodyweight Squat", "السكوات بوزن الجسم",
     "Front Quads", "bodyweight", "beginner", C, True),
    ("leg-press", "Leg Press", "دفع الأرجل على الآلة",
     "Front Quads", "machine", "beginner", C, False),
    ("leg-extension", "Leg Extension", "تمديد الأرجل على الآلة",
     "Front Quads", "machine", "beginner", I, False),
    ("dumbbell-walking-lunge", "Dumbbell Walking Lunge", "الطعنات المتحركة بالدمبل",
     "Front Quads", "dumbbell", "intermediate", C, True),
    ("bulgarian-split-squat", "Bulgarian Split Squat", "السكوات البلغاري بالدمبل",
     "Front Quads", "dumbbell", "intermediate", C, True),
    ("barbell-romanian-deadlift", "Barbell Romanian Deadlift", "الرفعة الرومانية بالبار",
     "Hamstrings", "barbell", "intermediate", C, False),
    ("dumbbell-romanian-deadlift", "Dumbbell Romanian Deadlift", "الرفعة الرومانية بالدمبل",
     "Hamstrings", "dumbbell", "beginner", C, True),
    ("lying-leg-curl", "Lying Leg Curl", "ثني الأرجل مستلقياً على الآلة",
     "Hamstrings", "machine", "beginner", I, False),
    ("barbell-hip-thrust", "Barbell Hip Thrust", "دفع الورك بالبار",
     "Glutes", "barbell", "intermediate", C, False),
    ("glute-bridge", "Glute Bridge", "جسر الأرداف",
     "Glutes", "bodyweight", "beginner", I, True),
    ("standing-calf-raise", "Standing Calf Raise", "رفع السمانة واقفاً على الآلة",
     "Calves", "machine", "beginner", I, False),
    ("single-leg-calf-raise", "Single-Leg Calf Raise", "رفع السمانة على ساق واحدة",
     "Calves", "bodyweight", "beginner", I, True),
    ("hip-adduction-machine", "Hip Adduction Machine", "ضم الفخذين على الآلة",
     "Adductors", "machine", "beginner", I, False),
    ("hip-abduction-machine", "Hip Abduction Machine", "فتح الفخذين على الآلة",
     "Abductors", "machine", "beginner", I, False),
    # ------------------------------------------------------------------- core
    ("crunch", "Crunch", "تمرين البطن كرنش",
     "Abdominals", "bodyweight", "beginner", I, True),
    ("plank", "Plank", "تمرين البلانك",
     "Abdominals", "bodyweight", "beginner", I, True),
    ("hanging-leg-raise", "Hanging Leg Raise", "رفع الأرجل معلقاً",
     "Abdominals", "bodyweight", "intermediate", I, False),
    ("russian-twist", "Russian Twist", "الالتفاف الروسي",
     "Obliques", "bodyweight", "beginner", I, True),
    ("side-plank", "Side Plank", "البلانك الجانبي",
     "Obliques", "bodyweight", "beginner", I, True),
    ("mountain-climber", "Mountain Climber", "تمرين متسلق الجبل",
     "Abdominals", "bodyweight", "beginner", C, True),
))
