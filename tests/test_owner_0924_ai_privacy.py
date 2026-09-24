"""The owner's decisions 1-3 of 2026-09-24 (P0): AI training copies are anonymous, are
kept for a limited time, and are deleted when the person opts out.

The consent and privacy screens promised that a chat kept to improve the coach carries
nothing that identifies the person. The rows held a user FK, the chat and message FKs,
and a snapshot with the user id, the exact age and weight and the injury free text.
"Don't patch the screen to hide an underlying contradiction; fix the model."
"""
import hashlib
import hmac
import importlib
import inspect
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.utils import timezone

pytestmark = pytest.mark.django_db

JSON = "application/json"

#: Keys that name or reach a person, which no stored snapshot or tool output may carry.
IDENTIFYING_KEYS = {"user_id", "id", "username", "email", "phone", "phone_number",
                    "first_name", "last_name", "full_name", "name", "trainer",
                    "created_by", "member_since", "session_id", "message_id", "injury",
                    "specific_injury", "goals", "age", "weight", "height", "bmi"}


def _person(make_user, tag, **kw):
    fields = dict(ai_training_consent=True, first_name="Rania", last_name="Haddad",
                  phone_number="0933123456", age=29, height=178.4, weight=83.2,
                  gender="Female", activity_level="Moderate",
                  specific_injury="torn ACL in my left knee after a car accident in Homs",
                  injury_areas=["knee"], client_goals=["lose belly fat before my wedding"],
                  training_goal="fat_loss")
    fields.update(kw)
    return make_user(f"{tag}_{uuid.uuid4().hex[:6]}", **fields)


def _turn(person, *, user_message="q", ai_response="a", tools_called=(),
          tool_results=()):
    """One answered turn through the real collector; returns (reply, the copy kept)."""
    from ai_assistant.models import AITrainingData, ChatMessage, ChatSession
    from ai_assistant.services.data_collector import DataCollector

    session = ChatSession.objects.create(user=person)
    ChatMessage.objects.create(session=session, role="user", content=user_message)
    reply = ChatMessage.objects.create(session=session, role="assistant",
                                       content=ai_response)
    DataCollector().log_interaction(
        user=person, session=session, user_message=user_message,
        ai_response=ai_response, tools_called=list(tools_called),
        tool_results=list(tool_results), message_id=reply.id)
    return reply, AITrainingData.objects.order_by("-id").first()


def _keys(value, into=None):
    into = set() if into is None else into
    if isinstance(value, dict):
        for key, item in value.items():
            into.add(key)
            _keys(item, into)
    elif isinstance(value, list):
        for item in value:
            _keys(item, into)
    return into


# ---------------------------------------------------------------- decision 1: the row
def test_a_copy_has_no_link_to_the_account_the_chat_or_the_message(make_user):
    """No FK at all; the one link is a pseudonym keyed with a server secret; nothing
    stored names the person."""
    from django.core.exceptions import ImproperlyConfigured
    from django.test import override_settings

    from ai_assistant.models import AITrainingData

    relations = [f.name for f in AITrainingData._meta.get_fields() if f.is_relation]
    assert relations == [], f"a training copy still links to {relations}"

    from ai_assistant.anonymise import reply_key_for, subject_key_for

    person = _person(make_user, "anonlink")
    reply, copy = _turn(person, user_message=f"it's {person.username}",
                        ai_response="Hi Rania Haddad")
    assert copy is not None

    secret = settings.AI_TRAINING_PSEUDONYM_KEY.encode()
    assert copy.subject_key == hmac.new(secret, f"subject:{person.pk}".encode(),
                                        hashlib.sha256).hexdigest()
    assert copy.reply_key == hmac.new(secret, f"reply:{reply.pk}".encode(),
                                      hashlib.sha256).hexdigest()
    # Keyed: a plain hash of the id, which anyone can recompute, is not what is stored,
    # and another secret gives another key.
    assert copy.subject_key != hashlib.sha256(str(person.pk).encode()).hexdigest()
    with override_settings(AI_TRAINING_PSEUDONYM_KEY="some-other-server-secret-value"):
        assert subject_key_for(person) != copy.subject_key
        assert reply_key_for(reply) != copy.reply_key
    with override_settings(AI_TRAINING_PSEUDONYM_KEY=""):
        with pytest.raises(ImproperlyConfigured):
            subject_key_for(person)

    stored = json.dumps({f.attname: getattr(copy, f.attname)
                         for f in AITrainingData._meta.concrete_fields
                         if f.attname not in ("subject_key", "reply_key")},
                        default=str, ensure_ascii=False)
    for needle in (person.username, person.email, "Rania", "Haddad", "0933123456",
                   "torn ACL", "wedding"):
        assert needle not in stored, needle
    assert not _keys(copy.user_context_snapshot) & IDENTIFYING_KEYS


def test_the_profile_is_kept_as_ranges_and_codes_only(make_user):
    """Age, height and weight as bands, BMI as a category, the injury as the areas
    chosen from the list and the goal as a code. Never a measurement or free text."""
    from ai_assistant.anonymise import (age_band, calorie_band, height_band,
                                        weight_band)

    person = _person(make_user, "anonbands")
    _reply, copy = _turn(person)
    assert copy.user_context_snapshot == {
        "age_band": "25-34", "height_band": "170-179", "weight_band": "80-89",
        "bmi_category": "overweight", "gender": "Female", "activity_level": "Moderate",
        "goal": "Lose", "training_goal": "fat_loss", "injury_areas": ["knee"],
        "active_routines": 0, "diet_goal": None, "daily_calories_band": None,
    }

    # The edges: a band `50-59` is 50 <= value < 60; the youngest share one band.
    assert [age_band(a) for a in (14, 17, 18, 24, 25, 64, 65, 90, None)] == [
        "<18", "<18", "18-24", "18-24", "25-34", "55-64", "65+", "65+", None]
    assert [weight_band(w) for w in (49.9, 50, 59.99, 60, 119.9, 120, 0)] == [
        "<50", "50-59", "50-59", "60-69", "110-119", "120+", None]
    assert [height_band(h) for h in (149, 150, 199.9, 200)] == [
        "<150", "150-159", "190-199", "200+"]
    assert [calorie_band(c) for c in (1100, 1200, 2000, 4000)] == [
        "<1200", "1200-1449", "1950-2199", "4000+"]


def test_the_text_and_the_tool_output_lose_every_direct_identifier(make_user):
    """E-mail addresses, phone numbers (Arabic-Indic digits too), links, @handles and
    the person's own names go from what they wrote and from the reply, and so do names
    a tool told the coach. Tool output loses ids and names, and the profile tool's answer
    becomes bands. Weights, dates and set counts stay."""
    person = _person(make_user, "raniah")
    message = (f"Hi, I am Rania Haddad ({person.username}). Mail rania.h@example.com "
               "or call +963 933 123 456 or ٠٩٣٣١٢٣٤٥٦. My plan: "
               "https://example.org/p/77 and insta @rania_fit. "
               "I lifted 60 70 80 90 kg on 2026-09-24, 3x10.")
    profile = {"name": "Rania Haddad", "age": 29, "height_cm": 178.4, "weight_kg": 83.2,
               "gender": "Female", "activity_level": "Moderate",
               "injury": "torn ACL in my left knee", "trainer": "Omar Saleh",
               "member_since": "2026-01-02", "goals": ["lose belly fat"]}
    diet = {"active_plan": {"id": 55, "goal": "Lose", "daily_calories": 1850,
                            "created_by": "Omar Saleh"}}
    history = {"sessions": [{"session_id": 9, "routine": "Rania's push day",
                             "exercise": "Bench Press", "weight_kg": 60}]}
    _reply, copy = _turn(
        person, user_message=message,
        ai_response="Well done Rania! Your coach Omar Saleh wants 4 sets of 8.",
        tools_called=["get_user_profile", "get_active_diet_plan", "get_workout_history"],
        tool_results=[profile, diet, history])

    text = copy.user_message
    for gone in ("Rania", "Haddad", person.username, "example.com", "933 123",
                 "٠٩٣٣", "example.org", "@rania_fit"):
        assert gone not in text, (gone, text)
    for token in ("[name]", "[email]", "[phone]", "[link]", "[handle]"):
        assert token in text, (token, text)
    for kept in ("60 70 80 90 kg", "2026-09-24", "3x10"):
        assert kept in text, (kept, text)

    for gone in ("Rania", "Omar", "Saleh"):
        assert gone not in copy.ai_response, copy.ai_response
    assert "4 sets of 8" in copy.ai_response

    banded, plan, sessions = copy.tool_results
    assert banded == {"age_band": "25-34", "height_band": "170-179",
                      "weight_band": "80-89", "gender": "Female",
                      "activity_level": "Moderate"}
    assert plan == {"active_plan": {"goal": "Lose", "daily_calories": 1850}}
    assert sessions == {"sessions": [{"routine": "[name]'s push day",
                                      "exercise": "Bench Press", "weight_kg": 60}]}
    assert copy.tools_called == ["get_user_profile", "get_active_diet_plan",
                                 "get_workout_history"]


def test_an_arabic_name_goes_with_the_letter_joined_to_it(make_user):
    person = _person(make_user, "anonar", first_name="رانيا", last_name="الحداد")
    _reply, copy = _turn(person, user_message="أنا رانيا الحداد ورقمي ٠٩٣٣١٢٣٤٥٦",
                         ai_response="أحسنت يا رانيا، وهذه الخطة لرانيا")
    assert "رانيا" not in copy.user_message and "الحداد" not in copy.user_message
    assert "٠٩٣٣١٢٣٤٥٦" not in copy.user_message
    assert "رانيا" not in copy.ai_response
    assert "ل[name]" in copy.ai_response, copy.ai_response


# ----------------------------------------------- decision 2: opt-out and every erasure
def test_switching_help_us_improve_off_deletes_that_persons_copies_only(make_user, api):
    from ai_assistant.models import AITrainingData

    mine = _person(make_user, "optout")
    theirs = _person(make_user, "optkeep")
    _turn(mine), _turn(mine), _turn(theirs)
    assert AITrainingData.copies_of(mine).count() == 2

    off = api(mine).post("/api/ai/training-consent/", {"granted": False},
                         content_type=JSON)
    assert off.status_code == 200, off.content
    assert AITrainingData.copies_of(mine).count() == 0
    assert AITrainingData.copies_of(theirs).count() == 1
    assert AITrainingData.objects.count() == 1


def test_the_data_summary_the_export_and_every_deletion_find_the_copies(make_user,
                                                                        api):
    """`GET/DELETE /api/ai/data/`, the privacy export, erasure and deleting the account
    all reach the copies through the pseudonym, and never another person's."""
    from ai_assistant.models import AITrainingData
    from wallet.models import Wallet

    person = _person(make_user, "anondata")
    other = _person(make_user, "anonother")
    _reply, copy = _turn(person, user_message="mail me at rania.h@example.com")
    _turn(person)
    _turn(other)
    client = api(person)

    summary = client.get("/api/ai/data/")
    assert summary.status_code == 200, summary.content
    assert summary.json()["details"]["training_records"] == 2

    export = client.get("/api/privacy/export/")
    assert export.status_code == 200
    rows = export.json()["sections"]["ai_training_data"]
    assert len(rows) == 2
    assert set(rows[0]) == {"created_at", "retain_until", "user_context_snapshot",
                            "user_message", "tools_called", "tool_results",
                            "ai_response", "user_feedback"}
    dumped = json.dumps(rows, ensure_ascii=False)
    assert copy.subject_key not in dumped and "rania.h@example.com" not in dumped

    deleted = client.delete("/api/ai/data/")
    assert deleted.status_code == 200
    assert deleted.json()["details"]["training_records"] == 2
    assert AITrainingData.copies_of(person).count() == 0
    assert AITrainingData.copies_of(other).count() == 1

    # "Help us improve" is still on after DELETE data/; the next turn is kept again.
    _turn(person)
    preview = client.get("/api/privacy/erase/").json()["preview"]["ai_training_data"]
    assert preview["rows"] == 1 and preview["action"] == "delete"
    erased = client.delete("/api/privacy/erase/?confirm=ERASE")
    assert erased.status_code == 200, erased.content
    assert AITrainingData.copies_of(person).count() == 0
    assert AITrainingData.copies_of(other).count() == 1

    # Deleting the account row itself: nothing cascades any more, the signal does it.
    gone = _person(make_user, "anongone")
    _turn(gone)
    pk = gone.pk
    Wallet.objects.filter(owner=gone).delete()
    gone.delete()
    assert AITrainingData.copies_of(pk).count() == 0
    assert AITrainingData.copies_of(other).count() == 1


def test_a_thumbs_up_after_the_copy_was_written_still_labels_it(make_user):
    from ai_assistant.models import AITrainingData

    person = _person(make_user, "anonfb")
    reply, copy = _turn(person)
    assert copy.user_feedback is None
    assert reply.record_feedback(person, "positive") == 1
    copy.refresh_from_db()
    assert copy.user_feedback == "positive"
    # Only through the owner's key: another person's key finds nothing.
    stranger = _person(make_user, "anonfbx")
    assert reply.record_feedback(stranger, "negative") == 0
    assert AITrainingData.objects.get(pk=copy.pk).user_feedback == "positive"


# ------------------------------------------------------ decision 3: every copy expires
def test_every_copy_has_an_expiry_and_the_daily_purge_deletes_it(make_user):
    from django.db import IntegrityError, transaction

    from ai_assistant.anonymise import subject_key_for
    from ai_assistant.models import AITrainingData
    from ai_assistant.tasks import purge_expired_training_data
    from training_platform.celery import app

    assert settings.AI_TRAINING_RETENTION_MAX_DAYS == 365
    days = settings.AI_TRAINING_RETENTION_DAYS
    assert 1 <= days <= settings.AI_TRAINING_RETENTION_MAX_DAYS

    person = _person(make_user, "anonttl")
    before = timezone.now()
    _reply, written = _turn(person)
    assert before + timedelta(days=days) <= written.retain_until \
        <= timezone.now() + timedelta(days=days)

    # A copy written any other way gets the same deadline, and none can be without one.
    bare = AITrainingData.objects.create(subject_key=subject_key_for(person),
                                         user_context_snapshot={}, user_message="q",
                                         ai_response="a")
    assert bare.retain_until is not None
    assert bare.retain_until <= timezone.now() + timedelta(days=days)
    with pytest.raises(IntegrityError), transaction.atomic():
        AITrainingData.objects.filter(pk=bare.pk).update(retain_until=None)

    AITrainingData.objects.filter(pk=written.pk).update(
        retain_until=timezone.now() - timedelta(seconds=1))
    purge_expired_training_data()
    assert not AITrainingData.objects.filter(pk=written.pk).exists()
    assert AITrainingData.objects.filter(pk=bare.pk).exists()

    # Scheduled, under the name the task is registered with.
    app.loader.import_default_modules()
    assert purge_expired_training_data.name in app.tasks
    assert any(entry["task"] == purge_expired_training_data.name
               for entry in app.conf.beat_schedule.values())


# ---------------------------------------------------------- the rows already kept
def test_the_migration_makes_existing_rows_anonymous_and_cannot_be_undone():
    from ai_assistant.anonymise import reply_key_for, subject_key_for

    migration = importlib.import_module(
        "ai_assistant.migrations.0009_training_copies_are_anonymous")
    assert all(op.reversible is False for op in migration.Migration.operations)
    dropped = importlib.import_module(
        "ai_assistant.migrations.0010_training_copies_lose_their_foreign_keys")
    assert {op.name for op in dropped.Migration.operations
            if type(op).__name__ == "RemoveField"} == {"user", "session", "message"}

    written = timezone.now() - timedelta(days=10)
    row = SimpleNamespace(
        user_id=42, message_id=7, retain_until=None, created_at=written,
        user_context_snapshot={
            "user_id": 42, "age": 29, "gender": "Female", "height": 178.4,
            "weight": 83.2, "activity_level": "Moderate",
            "goals": ["lose weight for my wedding"], "injury": "torn ACL", "bmi": 26.1,
            "active_routines": ["Rania push", "Rania pull"], "diet_goal": "Lose",
            "daily_calories": 1850},
        user_message="I'm Rania, rania@example.com", ai_response="Hi Rania Haddad",
        tools_called=["get_user_profile"],
        tool_results=[{"name": "Rania Haddad", "age": 29, "trainer": "Omar Saleh"}])
    migration.convert(row, ["Rania", "Haddad", "rania_h"], now=timezone.now(),
                      retention_days=365)

    assert row.subject_key == subject_key_for(42)
    assert row.reply_key == reply_key_for(7)
    assert row.user_context_snapshot == {
        "age_band": "25-34", "height_band": "170-179", "weight_band": "80-89",
        "bmi_category": "overweight", "gender": "Female", "activity_level": "Moderate",
        "goal": None, "training_goal": None, "injury_areas": [], "active_routines": 2,
        "diet_goal": "Lose", "daily_calories_band": "1700-1949",
    }
    assert row.user_message == "I'm [name], [email]"
    # "Rania Haddad" is also a name the profile tool reported: removed as one.
    assert row.ai_response == "Hi [name]"
    assert row.tool_results == [{"age_band": "25-34"}]
    assert row.retain_until == written + timedelta(days=365)


# ------------------------------------------------------------- the key and the contract
def test_production_refuses_to_boot_without_a_real_pseudonym_key():
    from tests.test_media_storage import _production_boot

    missing = _production_boot(AI_TRAINING_PSEUDONYM_KEY="")
    assert missing.returncode != 0
    assert "AI_TRAINING_PSEUDONYM_KEY" in missing.stderr, missing.stderr[-2000:]

    short = _production_boot(AI_TRAINING_PSEUDONYM_KEY="k" * 12)
    assert short.returncode != 0
    assert "AI_TRAINING_PSEUDONYM_KEY is too short" in short.stderr, short.stderr[-2000:]


def test_the_contract_says_what_a_copy_is_and_how_long_it_lives():
    from pathlib import Path

    from ai_assistant.views import AITrainingConsentView

    doc = inspect.getdoc(AITrainingConsentView)
    for words in ("ANONYMOUS", "keyed pseudonym", "365 days", "deletes every training copy",
                  "age band", "never the injury description"):
        assert words in doc, words

    contract = (Path(settings.BASE_DIR) / "API_CONTRACT.md").read_text()
    block = contract.split("**Anonymous AI training copies**", 1)
    assert len(block) == 2, "API_CONTRACT.md has no block for the anonymous copies"
    assert "365 days" in block[1].split("**Getting stronger**", 1)[0]
