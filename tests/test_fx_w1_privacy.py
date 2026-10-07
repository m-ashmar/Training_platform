"""Wave 1 of the owner's fix run (2026-09-28), the privacy rows.

Each test is the proof for one notebook row and fails on the code before its fix
(554c604):

* S-213 — erasure left the body measurements and a trainer's professional profile on
  the deactivated row, while the deletion page promised measurements are deleted and
  payment records are kept "without your name".
* S-214 — the profile export left out the photo, language, preferences, a trainer's
  card and the last sign-in, and carried the console's permission ids; a client's own
  program was only in their trainer's export.
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
