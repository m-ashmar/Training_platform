"""Four side bugs: the policy app's missing Arabic, and three around it.

One test per row of `SIDE_BUGS.md`, each written to fail against the behaviour that was
there before the fix:

| Row | What was wrong |
|---|---|
| S-019 | every user-facing string in `policy` went through gettext and NONE had Arabic, so the two refusals a store build hits reached people in English |
| S-024 | `subscriptions/current/` answered a 404 with `message` and no `code`, which the app cannot branch on |
| S-018 | a refused upload's evidence was written to `MEDIA_ROOT/quarantine` — the container's ephemeral disk once object storage went live, and the Celery worker has no volume at all |
| S-038 | `tests/security/phase7_write.py` posted two now-read-only fields and checked neither, so it read as proving a refused write while proving nothing |
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

CAPABILITIES = "/api/v1/config/capabilities"
CURRENT_SUBSCRIPTION = "/api/subscription/v1/subscriptions/current/"

#: The Arabic block, Arabic Supplement and Arabic Extended-A. One character from it in
#: the body is the whole claim: the string came from the catalogue, not from the source.
ARABIC = ("؀", "ࣿ")


def _has_arabic(text):
    return any(ARABIC[0] <= ch <= ARABIC[1] for ch in text)


def _body_text(response):
    """Every message the app could show, from one error body."""
    payload = response.json()
    return " ".join(str(payload.get(key, "")) for key in ("detail", "error", "message"))


# ------------------------------------------------------------------------- S-019
@pytest.mark.django_db
def test_a_store_builds_refusal_reaches_an_arabic_caller_in_arabic(make_user, api):
    """`policy` is the app that tells a caller "not in this version" and "say which
    build you are". Both strings went through gettext and neither had an entry in
    `locale/ar/LC_MESSAGES/django.po`, so gettext returned the msgid and an Arabic
    speaker — the primary audience — read English (SIDE_BUGS S-019).

    Asserted on the wire, in both directions: the `code` is unchanged and untranslated
    (the app branches on it), and the human text is Arabic.
    """
    from policy.defaults import DEFAULT_FLAGS
    from policy.models import ChannelCapabilities

    for channel, flags in DEFAULT_FLAGS.items():
        ChannelCapabilities.objects.get_or_create(
            channel=channel, min_version="0.0.0", defaults=flags)

    english = "This request must declare the app channel it comes from."
    client = api(make_user("s019_caller", user_type="client"))
    client.defaults["HTTP_ACCEPT_LANGUAGE"] = "ar"

    # 400 app_channel_required — a caller that did not say which build it is.
    undeclared = client.get(CAPABILITIES, HTTP_X_APP_CHANNEL="")
    assert undeclared.status_code == 400
    assert undeclared.json()["code"] == "app_channel_required"
    text = _body_text(undeclared)
    assert english not in text, text
    assert _has_arabic(text), text

    # 403 not_available_on_channel — the refusal a store build hits at a checkout.
    from decimal import Decimal

    from django.utils import translation

    from subscription.models import SubscriptionPlan

    with translation.override("en"):
        plan = SubscriptionPlan.objects.create(
            name="s019 plan", plan_type="premium", description="s019",
            price=Decimal("5000.00"), currency="SYP", duration_days=30, is_active=True)

    refused = client.post("/api/subscription/v1/subscriptions/",
                          data=json.dumps({"plan_id": plan.id}),
                          content_type="application/json", HTTP_X_APP_CHANNEL="ios")
    assert refused.status_code == 403
    assert refused.json()["code"] == "not_available_on_channel"
    text = _body_text(refused)
    assert "This is not available in this version of the app." not in text, text
    assert _has_arabic(text), text


def test_every_user_facing_string_in_policy_has_arabic():
    """The endpoint test above covers the two refusals; this covers the rest of the app,
    including the operator's help text, so a string added to `policy` without Arabic
    fails here with the file that holds it named.

    Borrows the gate's own two readers rather than re-implementing them, so the narrow
    check and the repo-wide one can never disagree about what "translated" means.
    """
    from tests.test_regression_gate import _first_party_msgids, _read_po

    catalogue = (ROOT / "locale/ar/LC_MESSAGES/django.po").read_text(encoding="utf-8")
    translated = set()
    for entry in _read_po(catalogue):
        if not entry["msgstrs"] or not all(s.strip() for s in entry["msgstrs"]):
            continue
        translated.update(m for m in (entry["msgid"], entry["msgid_plural"]) if m)

    untranslated = sorted(
        (where, msgid) for msgid, where in _first_party_msgids().items()
        if where.startswith("policy/") and msgid not in translated)
    assert not untranslated, untranslated


# ------------------------------------------------------------------------- S-024
@pytest.mark.django_db
def test_no_subscription_answers_a_code_the_app_can_branch_on(make_user, api):
    """The 404 carried `message` alone, so the app could not tell "this account has
    never subscribed" — show the plans — from any other 404 (SIDE_BUGS S-024). §2 of
    the contract: branch on `code`, never on text, because the text is translated.

    Only ADDED: `message` is still there, and the new code is `no_subscription`.
    """
    user = make_user("s024_client", user_type="client")
    response = api(user).get(CURRENT_SUBSCRIPTION)

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == "no_subscription", payload
    assert payload.get("message"), "the shipped `message` key was dropped, not added to"

    # And the code does not travel with the language: it is an identifier, not a message.
    arabic = api(user)
    arabic.defaults["HTTP_ACCEPT_LANGUAGE"] = "ar"
    answer = arabic.get(CURRENT_SUBSCRIPTION)
    assert answer.status_code == 404
    assert answer.json()["code"] == "no_subscription"


# ------------------------------------------------------------------------- S-018
@pytest.fixture
def quarantine_media(settings, tmp_path):
    """A media root of this test's own, so nothing lands in the repository."""
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def test_a_refused_uploads_evidence_goes_to_the_configured_store(quarantine_media, settings):
    """`MEDIA_ROOT/quarantine` was hardcoded, and with `MEDIA_BACKEND=gcs` live that is
    the container's ephemeral disk — the Celery worker, which runs the same validators,
    has no volume at all — so the proof of what an account sent was gone at the next
    restart (SIDE_BUGS S-018).

    Two claims, both of which the old code fails: the sample is written through the
    DEFAULT STORAGE, which is the bucket in production and the same object in the
    worker as in the web process; and WHERE it goes is `settings.QUARANTINE_LOCATION`,
    not a literal.
    """
    from django.core.files.storage import default_storage
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.core.exceptions import ValidationError

    from training_platform.file_security import FileSecurityValidator

    settings.QUARANTINE_LOCATION = "refused-uploads"

    upload = SimpleUploadedFile("payload.php", b"<?php echo 1; ?>",
                                content_type="application/x-php")
    with pytest.raises(ValidationError):
        FileSecurityValidator().validate_file(upload, file_type="image")

    # Read back through the storage API — the assertion that still holds on a bucket.
    _dirs, stored = default_storage.listdir("refused-uploads")
    assert len(stored) == 1, stored
    assert stored[0].startswith("quarantine_payload.php_"), stored
    assert default_storage.open(f"refused-uploads/{stored[0]}").read() == b"<?php echo 1; ?>"

    # And nowhere else: the old hardcoded path is not written to as well.
    assert not (quarantine_media / "quarantine").exists()

    # It really is the deployment's storage, so on `MEDIA_BACKEND=gcs` this same write
    # goes to the bucket rather than to a second local path the worker cannot see.
    assert FileSecurityValidator.quarantine_storage() is default_storage


def test_building_a_validator_does_not_touch_the_disk(quarantine_media, settings):
    """`__init__` ran an `os.makedirs` — once per validator, so once per upload, on a
    path that in production is the wrong filesystem anyway. The directory is now made
    by the storage, when something is actually written."""
    from training_platform.file_security import FileSecurityValidator

    settings.QUARANTINE_LOCATION = "refused-uploads"
    for _ in range(3):
        FileSecurityValidator()

    assert list(quarantine_media.iterdir()) == []


# ------------------------------------------------------------------------- S-038
def test_the_write_probe_asserts_that_the_derived_numbers_do_not_land():
    """`tests/security/phase7_write.py` posted `completed_sets`/`target_sets` to
    `user-exercise-progress/`. DRF drops a read-only field silently, so the POST is
    ACCEPTED and the values ignored — and the script printed a verdict about the row's
    OWNER while reading as though a refused write had been proven (SIDE_BUGS S-038).

    A probe that cannot fail is worse than no probe: it is a green light nobody earned.
    """
    source = (ROOT / "tests/security/phase7_write.py").read_text(encoding="utf-8")

    assert "completed_sets" in source, "the probe no longer exercises the derived fields"
    # It must READ BACK what was stored, not only the response...
    assert "row.completed_sets" in source and "row.target_sets" in source, source[-800:]
    # ...and it must fail the run, not print a line into a log nobody reads.
    assert "assert stored in (None,(0,0))" in source, source[-800:]


@pytest.mark.django_db
def test_a_client_cannot_author_their_own_compliance():
    """What the probe above now claims, asserted here too: the two numbers are derived
    (`completed_sets` from the set logs, `target_sets` from the coach's prescription),
    so a value sent by the app never reaches the row."""
    from routine.models import Exercise
    from routine.serializers import UserExerciseProgressSerializer

    exercise = Exercise.objects.create(name="s038 squat", description="x")
    serializer = UserExerciseProgressSerializer(data={
        "exercise": exercise.id, "date": "2026-09-21",
        "completed_sets": 7, "target_sets": 9,
    })
    assert serializer.is_valid(), serializer.errors
    assert "completed_sets" not in serializer.validated_data
    assert "target_sets" not in serializer.validated_data
