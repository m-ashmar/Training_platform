"""The owner, 2026-11-24: an e-mail when the password changes.

"Your password was changed; if this wasn't you, reset it and contact support", sent
through the notification e-mail channel (`notifications/channels/email.py`, kind
`password_changed`) after the commit, in the account's SAVED language, a plain-text and
an HTML part, and no link at all. A security notice: the notification preferences do not
stop it, as they do not stop the other account e-mails (the sign-up and reset codes,
`users/tasks.py`), and it is not among `email_event_types`.

Sent on a change while signed in and on a forgot-password reset; never on a refused
change. The 5-wrong-in-15-minutes limit of the change is untouched
(`tests/test_zz_road10_s28_guessing.py`). Each test fails on the code before, but the
refused change, which guards the rule.
"""
import uuid

import pytest
from django.core import mail

pytestmark = pytest.mark.django_db

JSON = "application/json"
OLD, NEW = "Xx!23456", "Brand-new-pass-91"
CHANGE = "/api/auth/password/change/"
ARABIC = range(0x0600, 0x0700)


def _name(tag):
    return f"pce_{tag}_{uuid.uuid4().hex[:6]}"


def _has_arabic(text):
    return any(ord(ch) in ARABIC for ch in text)


def _html(message):
    (html, mimetype), = message.alternatives
    assert mimetype == "text/html"
    return html


def _no_link(message):
    for part in (message.body, _html(message), message.subject):
        assert "http" not in part and "href" not in part and "www." not in part, part


def _change(api, person, captured, current=OLD):
    with captured(execute=True):
        return api(person).post(CHANGE, {"current_password": current, "new_password": NEW},
                                content_type=JSON)


def _sent(person):
    from notifications.models import EmailDelivery

    return list(EmailDelivery.objects.filter(recipient=person, kind="password_changed"))


def test_a_password_change_emails_the_person_in_english(
        make_user, api, django_capture_on_commit_callbacks, settings):
    settings.SUPPORT_EMAIL = "help@yalla.test"
    person = make_user(_name("en"), preferred_language="en", email_verified=True)
    assert _change(api, person, django_capture_on_commit_callbacks).status_code == 200

    (message,) = mail.outbox
    assert message.to == [person.email]
    assert "password" in message.subject.lower()
    assert "changed" in message.body and "wasn't you" in message.body
    assert "help@yalla.test" in message.body          # support, as text, not a link
    # It cannot be switched off, so it does not send the person to the settings.
    assert "notification settings" not in message.body
    assert "notification settings" not in _html(message)
    _no_link(message)
    (row,) = _sent(person)
    assert (row.status, row.language) == ("sent", "en")
    assert row.context["how"] == "change"


def test_a_password_change_emails_an_arabic_account_in_arabic(
        make_user, api, django_capture_on_commit_callbacks):
    person = make_user(_name("ar"), preferred_language="ar", email_verified=True)
    # The request's language is English; the e-mail follows the SAVED one.
    assert _change(api, person, django_capture_on_commit_callbacks).status_code == 200

    (message,) = mail.outbox
    assert _has_arabic(message.subject) and _has_arabic(message.body)
    assert _has_arabic(_html(message))
    _no_link(message)
    assert _sent(person)[0].language == "ar"


def test_the_notice_cannot_be_switched_off(make_user, api,
                                           django_capture_on_commit_callbacks):
    from notifications.models import UserNotificationPreference

    person = make_user(_name("muted"), email_verified=True)
    UserNotificationPreference.objects.create(user=person, event_type="all",
                                              is_enabled=False, channels={"email": False})
    assert _change(api, person, django_capture_on_commit_callbacks).status_code == 200
    assert len(mail.outbox) == 1 and _sent(person)[0].status == "sent"

    # A token minted from the object as it was would carry the old password's digest
    # and be refused (`training_platform/jwt_sessions.py`): read the new one first.
    person.refresh_from_db()
    types = api(person).get("/api/notifications/preferences/event_types/").json()
    assert "password_changed" not in types["email_event_types"]
    assert "password_changed" not in types["event_types"]


def test_a_forgot_password_reset_emails_the_person(make_user, api,
                                                   django_capture_on_commit_callbacks):
    from users.models import PasswordResetToken

    person = make_user(_name("reset"), email_verified=True)
    _token, raw = PasswordResetToken.issue(person, minutes=15)
    with django_capture_on_commit_callbacks(execute=True):
        done = api().post("/api/auth/forgot-password/confirm/",
                          {"reset_token": raw, "new_password": NEW}, content_type=JSON)
    assert done.status_code == 200, done.content

    (message,) = mail.outbox
    assert message.to == [person.email]
    _no_link(message)
    assert _sent(person)[0].context["how"] == "reset"


def test_a_refused_change_sends_nothing(make_user, api,
                                        django_capture_on_commit_callbacks):
    person = make_user(_name("wrong"), email_verified=True)
    refused = _change(api, person, django_capture_on_commit_callbacks, current="not-it")
    assert refused.status_code == 400
    assert mail.outbox == [] and _sent(person) == []


def test_each_change_is_its_own_notice(make_user, api,
                                       django_capture_on_commit_callbacks):
    person = make_user(_name("twice"), email_verified=True)
    assert _change(api, person, django_capture_on_commit_callbacks).status_code == 200
    person.refresh_from_db()
    with django_capture_on_commit_callbacks(execute=True):
        again = api(person).post(CHANGE, {"current_password": NEW,
                                          "new_password": "Another-pass-77"},
                                 content_type=JSON)
    assert again.status_code == 200, again.content
    assert len(mail.outbox) == 2 and len(_sent(person)) == 2
