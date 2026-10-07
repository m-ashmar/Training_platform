"""Wave 1 of the owner's fix run (2026-09-28), the privacy rows.

Each test is the proof for one notebook row and fails on the code before its fix
(554c604):

* S-213 — erasure left the body measurements and a trainer's professional profile on
  the deactivated row, while the deletion page promised measurements are deleted and
  payment records are kept "without your name".
* S-214 — the profile export left out the photo, language, preferences, a trainer's
  card and the last sign-in, and carried the console's permission ids; a client's own
  program was only in their trainer's export.
* S-231 — a test moved a privacy source to the end of the registry, and every later
  test rendered the data inventory in another order.
* S-225 — the code emails' footer printed no year; a dead helper wrote a payment
  outside `PaymentService`; a comment called behaviour events training data.
"""
from decimal import Decimal

import pytest
from django.utils import timezone

DELETE_ACCOUNT = "/legal/delete-account/"


# ================================================================================ S-213
@pytest.mark.django_db
def test_s213_erasure_clears_the_measurements_and_the_trainer_profile(make_user):
    """The page says "your measurements" are deleted. They were not: height, weight,
    age, gender and the activity level stayed on the anonymised row, and so did a
    trainer's bio, certifications and the rest of the card the public directory shows."""
    from training_platform.privacy import erase_user_data
    from users.models import CustomUser

    client = make_user("w1p_s213_c", height=181.0, weight=82.5, age=31, gender="Male",
                       activity_level="Active", activity_level_answered_at=timezone.now(),
                       client_preferences={"time": "evening"})
    trainer = make_user("w1p_s213_t", user_type="trainer",
                        trainer_bio="Ten years of coaching", trainer_specializations=["Strength"],
                        trainer_certifications=["NASM CPT"], trainer_experience_years=10,
                        trainer_hourly_rate=Decimal("15000.00"),
                        trainer_goal_focus=["strength"], trainer_injury_focus=["knee"],
                        trainer_training_places=["gym"])

    erase_user_data(client)
    erase_user_data(trainer)

    client = CustomUser.objects.get(pk=client.pk)
    assert (client.height, client.weight, client.age, client.gender) == \
        (None, None, None, None)
    assert client.activity_level_answered_at is None
    assert client.activity_level_unanswered, "the level reads as never asked"
    assert client.client_preferences == {}

    trainer = CustomUser.objects.get(pk=trainer.pk)
    assert trainer.trainer_bio in (None, "")
    assert (trainer.trainer_specializations, trainer.trainer_certifications,
            trainer.trainer_goal_focus, trainer.trainer_injury_focus,
            trainer.trainer_training_places) == ([], [], [], [], [])
    assert (trainer.trainer_experience_years, trainer.trainer_hourly_rate) == (None, None)
    assert not trainer.is_active


@pytest.mark.django_db
def test_s213_the_deletion_page_promises_no_more_than_erasure_does(client):
    """Payment records are kept attached to the anonymised account, but a withdrawal
    keeps the name it was paid to (`destination_holder`) and a transfer review keeps the
    sender's name and note: "without your name" was not true, in either language."""
    english = client.get(DELETE_ACCOUNT, {"lang": "en"}).content.decode()
    arabic = client.get(DELETE_ACCOUNT, {"lang": "ar"}).content.decode()

    assert "without your name" not in english
    assert "دون اسمك" not in arabic
    assert "A withdrawal keeps the account and the name it was paid to" in english
    assert "بالحساب والاسم اللذين دُفع إليهما" in arabic


# ================================================================================ S-214
@pytest.mark.django_db
def test_s214_the_profile_export_carries_what_the_person_chose_and_no_permission_ids(
        make_user):
    """The declared list left out what the person chose or wrote, and the M2M step
    attached `groups` and `user_permissions` to the profile whatever the list said."""
    from django.contrib.auth.models import Permission

    from training_platform.privacy import export_user_data

    signed_in = timezone.now()
    trainer = make_user("w1p_s214_t", user_type="trainer", preferred_language="ar",
                        trainer_bio="Strength coach", trainer_specializations=["Strength"],
                        trainer_certifications=["NASM CPT"],
                        client_preferences={"time": "evening"}, last_login=signed_in)
    trainer.user_permissions.add(Permission.objects.get(codename="view_customuser"))

    profile = export_user_data(trainer)["sections"]["profile"][0]

    for name in ("profile_picture", "preferred_language", "trainer_bio",
                 "trainer_specializations", "trainer_certifications", "client_preferences",
                 "last_login"):
        assert name in profile, f"{name} is not in the export"
    assert (profile["preferred_language"], profile["trainer_bio"],
            profile["trainer_specializations"], profile["trainer_certifications"],
            profile["client_preferences"], profile["last_login"]) == \
        ("ar", "Strength coach", ["Strength"], ["NASM CPT"], {"time": "evening"}, signed_in)
    assert "groups" not in profile and "user_permissions" not in profile, sorted(profile)


@pytest.mark.django_db
def test_s214_a_clients_export_carries_the_program_their_trainer_wrote_for_them(make_user):
    """A routine is one client's private copy (`routine/assignment.py`), reachable from
    them through `assigned_to`; registered on `created_by` alone it and its exercises
    were in the trainer's export and not in the client's."""
    from routine.models import Exercise, Routine, RoutineExercise
    from training_platform.privacy import export_user_data

    trainer = make_user("w1p_s214_coach", user_type="trainer")
    client = make_user("w1p_s214_client")
    other = make_user("w1p_s214_other")
    exercise = Exercise.objects.create(name="w1p s214 squat", created_by=trainer)

    mine = Routine.objects.create(name="w1p s214 month one", created_by=trainer, days=3)
    mine.assigned_to.add(client)
    theirs = Routine.objects.create(name="w1p s214 someone else", created_by=trainer, days=3)
    theirs.assigned_to.add(other)
    row = RoutineExercise.objects.create(routine=mine, exercise=exercise, day=1, sets=3,
                                         reps=10)
    RoutineExercise.objects.create(routine=theirs, exercise=exercise, day=1, sets=5, reps=5)

    sections = export_user_data(client)["sections"]
    assert [r["id"] for r in sections["routines_created"]] == [mine.pk]
    assert sections["routines_created"][0]["assigned_to"] == [client.pk]
    assert [r["id"] for r in sections["routine_exercises"]] == [row.pk]

    coach = export_user_data(trainer)["sections"]
    assert {r["id"] for r in coach["routines_created"]} == {mine.pk, theirs.pk}, \
        "the trainer still gets every routine they wrote"


# ================================================================================ S-231
def test_s231_the_mediakit_audit_test_leaves_the_registry_in_its_order():
    """`test_s028_the_privacy_audit_walks_mediakit_too` popped MediaAsset and re-added
    it, which moved it to the END of the registry for every later test in the process:
    the export, the erasure and `manage.py data_inventory` all walk the registry in
    order, and the inventory then no longer matched the committed file. Run as a
    module attribute, so pytest does not collect it a second time here."""
    from tests import test_side_bugs_round2 as round2
    from training_platform.privacy.registry import registry

    before = list(registry)
    held = registry
    round2.test_s028_the_privacy_audit_walks_mediakit_too()

    assert list(registry) == before, "the registry came back in another order"
    assert registry is held, "the registry is the same dict every module imported"


# ================================================================================ S-225
@pytest.mark.django_db
@pytest.mark.parametrize("send", ["send_otp_email", "send_password_reset_email"])
def test_s225_the_account_emails_print_the_year_in_their_footer(make_user, send):
    """`{{ "now"|date:"Y" }}` runs the `date` filter on the STRING "now", which is no
    date, so both code emails went out with "© " and no year. The notice email used the
    `{% now %}` tag and was right."""
    from django.core import mail

    from users import utils

    user = make_user(f"w1p_s225_{send}")
    mail.outbox = []

    getattr(utils, send)(user, "482913")

    sent = [m for m in mail.outbox if user.email in m.to]
    assert len(sent) == 1, [m.subject for m in mail.outbox]
    html = next(body for body, kind in sent[0].alternatives if kind == "text/html")
    assert f"&copy; {timezone.localtime().year} " in html, html[-600:]


def test_s225_the_helpers_write_no_payment_outside_the_service():
    """`subscription.utils.cancel_subscription` had no caller and wrote a `Payment` with
    `status='cancelled'` itself, with no `PaymentEvent` — the one thing only
    `PaymentService` may do. Dead, and a trap for whoever finds it: gone."""
    from pathlib import Path

    from subscription import utils

    assert not hasattr(utils, "cancel_subscription")
    source = Path(utils.__file__).read_text()
    assert "Payment.objects.create(" not in source


def test_s225_behaviour_events_are_not_called_training_data():
    """The owner's rule (2026-09-24): only what a person opted into ("help us improve",
    off by default) may train a model. The behaviour-event model said it was collected
    for "future model training", which is what a later reader would build on."""
    import inspect

    from ai_assistant import models

    assert "future model training" not in inspect.getsource(models)
    assert "NOT training data" in models.UserBehaviorEvent.__doc__
