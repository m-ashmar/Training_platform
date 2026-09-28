"""Wave 1 of the owner's fix run (2026-09-28), the privacy rows.

Each test is the proof for one notebook row and fails on the code before its fix
(554c604):

* S-213 — erasure left the body measurements and a trainer's professional profile on
  the deactivated row, while the deletion page promised measurements are deleted and
  payment records are kept "without your name".
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
