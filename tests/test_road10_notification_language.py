"""Road to 10, notification language P1 (2026-11-24; the manager's ruling under the
owner's delegation): every inbox row reads in the REQUEST's language, whatever language
it was written in.

Five kinds stored a finished sentence (body `%(message)s`) in the recipient's SAVED
language — one with no language at all, so every workout reminder was English (F1).
Four of them now also name the sentence by its key on the event's template
(`notifications/domain/routine_events.py` `variants`) and its params; the one rendering
path (`NotificationTemplateResolver.render`, after the build gate's `choose`) renders
the variant in the ACTIVE language: the request's in the inbox, the saved one in a push.
A row with no variant reads its stored `message`, as before. `custom` (the trainer's own
words) has none.

Every test here fails on the code before this change except three: the two fallbacks
(what old rows did, kept) and the build-and-language one (the gated kinds composed
before; kept as the proof the variants did not break it).
"""
import types
import uuid

import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"
INBOX = "/api/social/notifications/"
TOKENS = "/api/auth/device-token/"

#: (kind, the row's context without `message`, English, Arabic): one row per variant
#: family, the Arabic as the catalogue has it (locale/ar/LC_MESSAGES/django.po).
ROWS = [
    ("routine_assignment", {"variant": "assigned", "routine": "Power"},
     "A new routine, Power, has been assigned to you.", "تم تعيين برنامج جديد لك: Power."),
    ("session_completed", {"variant": "client", "routine": "Power"},
     "Well done! You finished a workout on Power.", "أحسنت! أنهيت تمريناً من Power."),
    ("session_completed", {"variant": "trainer", "name": "Sami Ali", "routine": "Power"},
     "Sami Ali finished a workout on Power.", "أنهى Sami Ali تمريناً من Power."),
    ("progress_milestone", {"variant": "streak", "days": 7},
     "7 days in a row. Keep it going.", "7 يومًا متتاليًا. واصل."),
    ("session_reminder", {"variant": "scheduled", "day": 2, "routine": "Power"},
     "Day 2 of Power is waiting for you.", "اليوم 2 من Power بانتظارك."),
    ("session_reminder", {"variant": "days", "days": 3},
     "It has been 3 days since your last workout.", "مضى 3 يومًا منذ آخر تمرين لك."),
]

#: Params for every variant, to render each in both languages.
PARAMS = {"routine": "Power", "name": "Sami Ali", "day": 2, "days": 3, "count": 10}


def _name(tag):
    return f"lang_{tag}_{uuid.uuid4().hex[:6]}"


def _row(person, kind, context, message):
    from notifications.models import Notification

    return Notification.objects.create(
        recipient=person, event_type=kind, deduplication_key=uuid.uuid4().hex,
        related_object_id=uuid.uuid4().hex,
        metadata={"context": {**context, "message": message}, "data": {"type": kind}})


def _read(api, person, lang, build="apk"):
    """{notification id: (title, message)} as this build reads it in `lang`."""
    headers = {"HTTP_ACCEPT_LANGUAGE": lang, "HTTP_X_APP_CHANNEL": build,
               "HTTP_X_APP_VERSION": "1.0.0"}
    body = api(person).get(f"{INBOX}?page_size=100", **headers).json()
    return {row["id"]: (row["title"], row["message"]) for row in body["results"]}


@pytest.fixture
def pushes(monkeypatch):
    """Every multicast FCM is handed: [(token, title, body)]."""
    from firebase_admin import messaging

    sent = []

    def send_each_for_multicast(message, dry_run=False):
        for token in message.tokens:
            sent.append((token, message.notification.title, message.notification.body))
        return types.SimpleNamespace(
            responses=[types.SimpleNamespace(success=True, exception=None)
                       for _ in message.tokens],
            success_count=len(message.tokens), failure_count=0)

    monkeypatch.setattr(messaging, "send_each_for_multicast", send_each_for_multicast)
    return sent


def _register(api, person, tag):
    token = f"tok-{tag}-{uuid.uuid4().hex}"
    answered = api(person).post(TOKENS, {"token": token, "platform": "android"},
                                content_type=JSON, HTTP_X_APP_CHANNEL="apk",
                                HTTP_X_APP_VERSION="1.0.0")
    assert answered.status_code in (200, 201), answered.content
    return token


def _bodies(sent, token):
    return [body for tok, _title, body in sent if tok == token]


# ------------------------------------------------------------------ one row, two languages
def test_each_variant_kind_reads_in_the_request_language(make_user, api):
    """One stored row per variant, written for an English account (its stored `message`
    English), read in Arabic and in English: the body follows the request, as the title
    always did."""
    person = make_user(_name("rows"), preferred_language="en")
    rows = {_row(person, kind, context, english).pk: (kind, english, arabic)
            for kind, context, english, arabic in ROWS}

    arabic_inbox, english_inbox = _read(api, person, "ar"), _read(api, person, "en")
    for pk, (kind, english, arabic) in rows.items():
        assert arabic_inbox[str(pk)][1] == arabic, (kind, arabic_inbox[str(pk)])
        assert english_inbox[str(pk)][1] == english, (kind, english_inbox[str(pk)])
    completed = next(pk for pk, (kind, *_rest) in rows.items() if kind == "session_completed")
    assert arabic_inbox[str(completed)][0] == "اكتمل التمرين"


def test_every_variant_has_its_arabic_and_renders_with_its_params():
    """Every variant of the four kinds renders in both languages from the params its
    writer stores, and the Arabic is a translation, not the English again."""
    from django.utils import translation

    from notifications.domain.routine_events import (ProgressMilestoneEvent,
                                                     RoutineAssignmentEvent,
                                                     SessionCompletedEvent,
                                                     SessionReminderEvent)

    for event in (RoutineAssignmentEvent, SessionCompletedEvent, SessionReminderEvent,
                  ProgressMilestoneEvent):
        assert event.template.variants, event
        for key, body in event.template.variants.items():
            with translation.override("en"):
                english = str(body) % PARAMS
            with translation.override("ar"):
                arabic = str(body) % PARAMS
            assert arabic != english, (event.__name__, key, english)


def test_the_writers_name_only_variants_their_template_offers():
    """The keys the writers store are the template's: a milestone at every threshold,
    each reminder branch."""
    from notifications import milestones
    from notifications.domain.routine_events import (ProgressMilestoneEvent,
                                                     SessionReminderEvent)

    offered = ProgressMilestoneEvent.template.variants
    for days in milestones.STREAK_MILESTONES:
        assert milestones._streak_variant(days)[0] in offered
    for count in milestones.SESSION_MILESTONES:
        assert milestones._session_variant(count)[0] in offered
    assert {"scheduled", "yesterday", "days", "days_long"} <= set(
        SessionReminderEvent.template.variants)


# ------------------------------------------------------------------ the fallbacks
def test_a_row_with_no_variant_reads_its_stored_message(make_user, api):
    """A row written before (context `{"message": …}` only) reads its stored sentence on
    every language, as it always did; so does `custom`, the trainer's own words."""
    person = make_user(_name("old"))
    old = _row(person, "session_completed", {}, "Stored before the change.")
    custom = _row(person, "custom", {}, "كلمات المدرب كما كتبها")
    for lang in ("ar", "en"):
        inbox = _read(api, person, lang)
        assert inbox[str(old.pk)][1] == "Stored before the change."
        assert inbox[str(custom.pk)][1] == "كلمات المدرب كما كتبها"


def test_a_variant_no_longer_offered_reads_its_stored_message(make_user, api):
    person = make_user(_name("gone"))
    gone = _row(person, "session_reminder", {"variant": "no_such_variant", "days": 3},
                "The stored reminder.")
    broken = _row(person, "session_reminder", {"variant": "scheduled"},  # params missing
                  "The stored scheduled reminder.")
    inbox = _read(api, person, "ar")
    assert inbox[str(gone.pk)][1] == "The stored reminder."
    assert inbox[str(broken.pk)][1] == "The stored scheduled reminder."


# ------------------------------------------------------------------ build and language
def test_the_build_gate_and_the_language_compose(make_user, api):
    """The build picks the template (R1), the request's language renders it: an ios
    request in Arabic reads the STORE texts in Arabic, an apk one the defaults."""
    person = make_user(_name("gate"))
    due = _row(person, "coaching_payment_due", {"name": "Rami"}, "")
    lapsed = _row(person, "subscription_expired", {}, "")
    checkout = _row(person, "checkout_expired", {}, "")

    ios = _read(api, person, "ar", build="ios")
    assert ios[str(due.pk)][1] == \
        "تدريبك مع Rami متوقف مؤقتاً. لا يزال بإمكانك الاطلاع على برامجك."
    assert ios[str(lapsed.pk)][1] == \
        "خطتك الغذائية وبرنامجك التدريبي محفوظان ويمكنك الاطلاع عليهما."
    assert str(checkout.pk) not in ios

    apk = _read(api, person, "ar", build="apk")
    assert apk[str(due.pk)][1] == \
        "تدريبك مع Rami متوقف حتى يُدفع الشهر التالي. لا يزال بإمكانك الاطلاع على برامجك."
    assert apk[str(lapsed.pk)][1] != ios[str(lapsed.pk)][1]
    assert str(checkout.pk) in apk


# ------------------------------------------------------------------ F1, and the push
def test_an_arabic_accounts_workout_reminder_reads_arabic_in_the_inbox_and_the_push(
        make_user, api, pushes, django_capture_on_commit_callbacks):
    """F1: the reminder was rendered with no language set, so it was English in the
    inbox and in the push, Arabic accounts included. Now the inbox reads it in the
    request's language, and FCM is handed the saved language's."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from django.utils import timezone, translation

    from notifications.models import Notification
    from notifications.tasks import send_workout_reminders
    from routine.models import Routine, WorkoutSession

    trainer = make_user(_name("rem_t"), user_type="trainer")
    routine = Routine.objects.create(
        name="Power", created_by=trainer,
        start_date=timezone.localdate() - timedelta(days=60),
        end_date=timezone.localdate() - timedelta(days=30))
    hour = timezone.now().astimezone(ZoneInfo("Asia/Damascus")).hour
    person = make_user(_name("rem"), preferred_language="ar",
                       preferred_timezone="Asia/Damascus", workout_reminder_hour=hour)
    routine.assigned_to.add(person)
    session = WorkoutSession.objects.create(user=person, routine=routine, status="completed")
    WorkoutSession.objects.filter(pk=session.pk).update(
        start_time=timezone.now() - timedelta(days=3))
    token = _register(api, person, "rem")

    # As on the worker: no request has activated a language in its thread. (Here the
    # token request above left the account's Arabic active, which hid F1.)
    translation.deactivate()
    with django_capture_on_commit_callbacks(execute=True):
        send_workout_reminders()

    row = Notification.objects.get(recipient=person, event_type="session_reminder")
    arabic = "مضى 3 يومًا منذ آخر تمرين لك."
    assert _read(api, person, "ar")[str(row.pk)] == ("تذكير بالتمرين", arabic)
    assert _read(api, person, "en")[str(row.pk)][1] == \
        "It has been 3 days since your last workout."
    assert _bodies(pushes, token) == [arabic]


def test_a_push_is_in_the_saved_language_and_the_inbox_in_the_readers(
        make_user, api, pushes, django_capture_on_commit_callbacks):
    """A finished workout, through the real route: the Arabic client is pushed Arabic,
    the English trainer English (each one's saved language); the client's own row,
    read with `Accept-Language: en`, reads English."""
    from notifications.models import Notification
    from routine.models import Routine, WorkoutSession
    from users.models import TrainerClientRelation

    trainer = make_user(_name("done_t"), user_type="trainer", preferred_language="en")
    client = make_user(_name("done_c"), preferred_language="ar",
                       first_name="Sami", last_name="Ali")
    TrainerClientRelation.objects.create(trainer=trainer, client=client, status="approved")
    routine = Routine.objects.create(name="Power", created_by=trainer, days=3)
    session = WorkoutSession.objects.create(user=client, routine=routine, day=1)
    mine, theirs = _register(api, client, "done_c"), _register(api, trainer, "done_t")

    with django_capture_on_commit_callbacks(execute=True):
        answered = api(client).patch(f"/api/routine/workout-sessions/{session.pk}/",
                                     {"status": "completed"}, content_type=JSON)
    assert answered.status_code == 200, answered.content

    # The client's first workout also earns a milestone push; both are Arabic.
    assert "أحسنت! أنهيت تمريناً من Power." in _bodies(pushes, mine)
    assert "Well done! You finished a workout on Power." not in _bodies(pushes, mine)
    assert _bodies(pushes, theirs) == ["Sami Ali finished a workout on Power."]
    row = Notification.objects.get(recipient=client, event_type="session_completed")
    assert row.metadata["context"]["variant"] == "client"
    assert _read(api, client, "en")[str(row.pk)][1] == \
        "Well done! You finished a workout on Power."
    assert _read(api, client, "ar")[str(row.pk)][1] == "أحسنت! أنهيت تمريناً من Power."
