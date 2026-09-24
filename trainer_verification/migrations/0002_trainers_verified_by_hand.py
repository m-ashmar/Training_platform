"""Trainers an operator verified by hand before applications existed stay verified.

Until this app, `trainer_is_verified` was a checkbox in the console. Those trainers keep
the flag (nothing here touches it) and get an application in state `verified` plus one
decision row saying how they got there, with no documents. Why a row and not only the
flag: the flag and the application now mean the same thing, and every screen that
explains a verification — the console's list, the trainer's own
`GET /api/v1/trainer/verification/`, a later revocation — works from the application.
Without a row, a legacy-verified trainer would be invisible in the console and could not
be revoked from it. (`services.status_for` still answers `verified` for a flag with no
row, so a row created any other way cannot disagree with the flag either.)

Reversible: the reverse deletes exactly the rows this wrote (round 0, no decider).
"""
from django.db import migrations
from django.utils import timezone

NOTE = ("Verified by hand in the console before verification applications existed; "
        "no documents on file.")


def forwards(apps, schema_editor):
    User = apps.get_model("users", "CustomUser")
    Application = apps.get_model("trainer_verification", "VerificationApplication")
    Decision = apps.get_model("trainer_verification", "VerificationDecision")
    now = timezone.now()
    for trainer_id in (User.objects.filter(user_type="trainer", trainer_is_verified=True)
                       .order_by("id").values_list("id", flat=True)):
        if Application.objects.filter(trainer_id=trainer_id).exists():
            continue
        application = Application.objects.create(
            trainer_id=trainer_id, state="verified", round=0, decided_at=now)
        Decision.objects.create(application=application, round=0, outcome="verified",
                                reason=NOTE, documents=[], decided_at=now)


def backwards(apps, schema_editor):
    Application = apps.get_model("trainer_verification", "VerificationApplication")
    Application.objects.filter(round=0, state="verified", decided_by__isnull=True,
                               decisions__reason=NOTE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("trainer_verification", "0001_initial"),
        ("users", "0046_customuser_ai_training_consent_set_at"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
