"""BACKEND_BUGS B-168: no document calls a registered push "email only".

`payment_confirmed` became a push (`notifications/channels/fcm.py` registers it, and
`notifications/listeners/money_listeners.py` sends it through
`NotificationService.create_and_send`), but the contract's email table still said
«Email only: there is no push of this type», the preferences' docstrings said it «has no
push and is in this list only», and the email channel's comment said only the `all` row
reaches its email. The push goes like every other registered push; the type's own
preference row governs it (`channels.fcm`) and the receipt email (`channels.email`).

On ccd5da0 the three documents still say so, and the first test fails on each.
"""
import inspect
import re
from pathlib import Path

import pytest

CONTRACT = Path(__file__).resolve().parent.parent / "API_CONTRACT.md"


def _email_table_row(contract: str, event_type: str) -> str:
    """The row of the contract's email table (`| preference event_type | the email |`)
    that names `event_type`."""
    head = contract.index("| preference `event_type` | the email |")
    table = contract[head:contract.index("\n\n", head)]
    rows = [line for line in table.splitlines() if f"`{event_type}`" in line.split("|")[1]]
    assert len(rows) == 1, (event_type, rows)
    return rows[0]


def test_b168_no_doc_calls_a_registered_push_email_only():
    from notifications.channels import email as email_channel
    from notifications.channels.email import email_preference_types
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY
    from notifications.views import NotificationPreferenceViewSet

    assert "payment_confirmed" in EVENT_CLASS_REGISTRY
    contract = CONTRACT.read_text(encoding="utf-8")
    both = [t for t in email_preference_types() if t in EVENT_CLASS_REGISTRY]
    assert "payment_confirmed" in both

    for event_type in both:
        row = _email_table_row(contract, event_type)
        assert not re.search(r"no push", row, re.I), row
        assert not re.search(r"email only", row, re.I), row

    # The receipt's row says which push goes with it, and that a routine product's
    # checkout pushes something else.
    receipt_row = _email_table_row(contract, "payment_confirmed")
    assert "push `payment_confirmed`" in receipt_row, receipt_row
    assert "routine_product_purchased" in receipt_row, receipt_row

    for doc in (NotificationPreferenceViewSet.__doc__,
                NotificationPreferenceViewSet.event_types.__doc__):
        assert "no push" not in doc, doc
        assert "email only" not in doc.lower(), doc

    # The channel's own comment named the `all` row as the only one that reaches the
    # receipt email.
    source = inspect.getsource(email_channel)
    assert "until a push of that type exists" not in source
    assert "only the `all` preference row reaches it" not in source


@pytest.mark.django_db
def test_b168_the_type_row_governs_the_push_and_the_receipt_email_apart(make_user):
    """What the corrected documents say: the `payment_confirmed` row is the receipt's
    push (`channels.fcm`) and its email (`channels.email`), each on its own, and
    `is_enabled: false` stops both."""
    from notifications.channels.email import refusal
    from notifications.models import UserNotificationPreference

    user = make_user("b168_prefs", email_verified=True)
    pref =UserNotificationPreference.objects.create(
        user=user, event_type="payment_confirmed", channels={"fcm": False})
    assert not UserNotificationPreference.is_channel_enabled(user, "payment_confirmed", "fcm")
    assert refusal(user, "payment_confirmed") is None

    pref.channels = {"email": False}
    pref.save(update_fields=["channels"])
    assert UserNotificationPreference.is_channel_enabled(user, "payment_confirmed", "fcm")
    assert refusal(user, "payment_confirmed") == "muted"

    pref.channels = {}
    pref.is_enabled = False
    pref.save(update_fields=["channels", "is_enabled"])
    assert not UserNotificationPreference.is_channel_enabled(user, "payment_confirmed", "fcm")
    assert refusal(user, "payment_confirmed") == "muted"


def test_b168_the_event_types_answer_lists_every_email_type_as_a_push_too(
        make_user, api, db):
    """`event_types/` lists each email type in `event_types` as well, as its docstring
    now says."""
    user = make_user("b168_types")
    resp = api(user).get("/api/notifications/preferences/event_types/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert "payment_confirmed" in body["email_event_types"]
    assert set(body["email_event_types"]) <= set(body["event_types"]), body
