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
* B-174 — production booted with no `PUBLIC_PAGES_BASE_URL`, and the legal links every
  build is handed followed whatever host a request reached.
* S-233 — no command re-saved the encrypted columns, so an old FIELD_ENCRYPTION_KEY
  could never be dropped.
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


# ================================================================================ B-174
@pytest.mark.parametrize("base, needle", [
    ("", "PUBLIC_PAGES_BASE_URL is not set"),
    ("http://yallagym.example", "must be an https URL"),
    ("yallagym.example", "must be an https URL"),
    ("https://localhost:8000", "a local address in production"),
])
def test_b174_production_refuses_to_boot_without_the_public_pages_host(base, needle):
    """Unset, the capabilities links (privacy policy, terms, account deletion) were built
    on whatever allowed host a request reached — the API's own address, not the hub
    domain the store listing names. The owner (2026-09-28): production refuses to boot
    without an https base. The shared production environment is otherwise bootable."""
    from tests.test_media_storage import GCS_KEY_JSON, _production_boot

    bootable = dict(MEDIA_BACKEND="gcs", GS_BUCKET_NAME="yalla-gym-media",
                    GS_CREDENTIALS_JSON=GCS_KEY_JSON)
    refused = _production_boot(PUBLIC_PAGES_BASE_URL=base, **bootable)

    assert refused.returncode != 0 and "BOOTED" not in refused.stdout, refused.stdout
    assert needle in refused.stderr, refused.stderr[-2000:]

    booted = _production_boot(PUBLIC_PAGES_BASE_URL="https://yallagym.example", **bootable)
    assert booted.returncode == 0 and "BOOTED" in booted.stdout, booted.stderr[-2000:]


# ================================================================================ S-233
def _raw(model, pk, column):
    """The stored text of an encrypted column, without the field's own decryption."""
    from django.db import models
    from django.db.models.functions import Cast

    return (model._base_manager.filter(pk=pk)
            .annotate(raw=Cast(column, output_field=models.TextField()))
            .values_list("raw", flat=True).get())


@pytest.mark.django_db
def test_s233_a_rotation_rewrites_every_encrypted_value_so_the_old_key_can_go(
        make_user, settings):
    """The documented rotation was "prepend the new key, re-save the rows, drop the old
    one", and nothing re-saved the rows: every value written under the old key kept
    needing it, so it could never be dropped. After `reencrypt_fields` the old key is
    removed and every value — on more than one model — still reads back."""
    import io

    from cryptography.fernet import Fernet
    from django.core.management import call_command

    from subscription.models import PayerAddress
    from users.models import CustomUser

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = old
    user = make_user("w1p_s233_rot", specific_injury="ACL reconstruction 2023",
                     injury_areas=["knee", "lower_back"])
    payer = PayerAddress.objects.create(user=user, address="0912345678",
                                        address_digest="w1p" * 21 + "x")
    assert Fernet(old.encode()).decrypt(
        _raw(CustomUser, user.pk, "specific_injury").encode()) == b"ACL reconstruction 2023"

    settings.FIELD_ENCRYPTION_KEY = f"{new},{old}"
    out = io.StringIO()
    call_command("reencrypt_fields", stdout=out)
    assert "users.CustomUser.specific_injury: 1 re-encrypted" in out.getvalue(), \
        out.getvalue()

    settings.FIELD_ENCRYPTION_KEY = new  # the old key is dropped
    user = CustomUser.objects.get(pk=user.pk)
    assert user.specific_injury == "ACL reconstruction 2023"
    assert user.injury_areas == ["knee", "lower_back"]
    assert PayerAddress.objects.get(pk=payer.pk).address == "0912345678"
    assert Fernet(new.encode()).decrypt(
        _raw(PayerAddress, payer.pk, "address").encode()) == b"0912345678"


@pytest.mark.django_db
def test_s233_a_value_no_listed_key_opens_is_left_alone_and_fails_the_rotation(
        make_user, settings):
    """Re-saving through the field would have wrapped such a value in a second layer
    (the field hands an unreadable token back unchanged), and a run that says nothing
    lets the operator drop a key a row still needs. Plain text left from before a
    column was encrypted is encrypted. A dry run writes nothing."""
    import io

    from cryptography.fernet import Fernet
    from django.core.management import CommandError, call_command
    from django.db import models

    from users.models import CustomUser

    lost, current = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = lost
    orphan = make_user("w1p_s233_lost", specific_injury="old knee")
    settings.FIELD_ENCRYPTION_KEY = current
    legacy = make_user("w1p_s233_plain")
    CustomUser.objects.filter(pk=legacy.pk).update(
        specific_injury=models.Value("bad back", output_field=models.TextField()))
    orphan_token = _raw(CustomUser, orphan.pk, "specific_injury")

    with pytest.raises(CommandError, match=r"1 value\(s\)"):
        call_command("reencrypt_fields", "--dry-run", stdout=io.StringIO())
    assert _raw(CustomUser, legacy.pk, "specific_injury") == "bad back"

    with pytest.raises(CommandError, match=rf"users.CustomUser.specific_injury pk={orphan.pk}"):
        call_command("reencrypt_fields", stdout=io.StringIO())
    assert _raw(CustomUser, orphan.pk, "specific_injury") == orphan_token
    assert Fernet(current.encode()).decrypt(
        _raw(CustomUser, legacy.pk, "specific_injury").encode()) == b"bad back"
