"""Road to 10 S0.4: the account pages answer a TRAINER correctly.

The app could not reach these pages as a trainer until F0.2, so nothing had asked them.
Checked: the inbox, data export, blocked users and the delete-account refusal while
coaching already answered a trainer as documented (kept here as guards). Fixed, and
failing on the code before this change: the notification settings offered a trainer every
kind the platform sends, the plan's and the workout's included
(`event_types/` now says which kinds the caller's ROLE receives, `role_event_types`). AI
consent answers a trainer as a client and offers nothing of its own; the app hides the AI
rows by role.
"""
import pytest

pytestmark = pytest.mark.django_db

JSON = "application/json"


def test_every_notification_kind_has_an_audience():
    from notifications.audience import AUDIENCE
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    assert set(EVENT_CLASS_REGISTRY) == set(AUDIENCE)


def test_a_trainers_notification_settings_list_a_trainers_kinds(make_user, api):
    from notifications.channels.fcm import EVENT_CLASS_REGISTRY

    trainer = make_user("s04_kinds_t", user_type="trainer")
    body = api(trainer).get("/api/notifications/preferences/event_types/").json()

    # Unchanged for the builds that read it: every kind, and `all`.
    assert body["event_types"] == sorted(EVENT_CLASS_REGISTRY) + ["all"]
    mine = set(body["role_event_types"])
    assert body["role"] == "trainer"
    assert {"all", "client_request_received", "client_request_cancelled",
            "client_unassigned_trainer", "coaching_fee_released", "chat_message",
            "withdrawal_paid", "trainer_verification_approved", "routine_product_sold",
            "trainer_weekly_digest", "session_completed"} <= mine
    assert not {"session_reminder", "subscription_expiring", "payment_confirmed",
                "routine_assignment", "weekly_recap", "wallet_topped_up",
                "coaching_payment_due", "trainer_assignment_request"} & mine
    assert body["role_email_event_types"] == ["withdrawal_failed", "withdrawal_paid",
                                              "withdrawal_requested"]

    saved = api(trainer).post("/api/notifications/preferences/",
                              {"event_type": "client_request_received",
                               "is_enabled": False}, content_type=JSON)
    assert saved.status_code == 201, saved.content


def test_a_clients_notification_settings_list_a_clients_kinds(make_user, api):
    client = make_user("s04_kinds_c")
    body = api(client).get("/api/notifications/preferences/event_types/").json()
    mine = set(body["role_event_types"])
    assert {"all", "session_reminder", "subscription_expiring", "chat_message"} <= mine
    assert not {"withdrawal_paid", "client_request_received", "routine_product_sold"} & mine
    assert "withdrawal_paid" not in body["role_email_event_types"]
    assert "payment_confirmed" in body["role_email_event_types"]


def test_nothing_offers_a_trainer_an_ai_row(make_user, api):
    """No notification kind is about the AI coach, and the coach itself is a client's
    plan feature: a trainer without a plan is refused before consent is asked."""
    trainer = make_user("s04_ai_t", user_type="trainer")
    kinds = api(trainer).get("/api/notifications/preferences/event_types/").json()
    assert not [kind for kind in kinds["role_event_types"] if "ai" in kind.split("_")]
    assert api(trainer).get("/api/v1/ai-consent/").status_code == 200


def test_a_trainers_inbox_export_and_blocks_answer(make_user, api):
    trainer = make_user("s04_pages_t", user_type="trainer")
    other = make_user("s04_pages_o")

    assert api(trainer).get("/api/social/notifications/").status_code == 200
    exported = api(trainer).get("/api/privacy/export/")
    assert exported.status_code == 200
    assert api(trainer).get("/api/v1/blocks/").status_code == 200
    blocked = api(trainer).post("/api/v1/blocks/", {"user_id": other.pk},
                                content_type=JSON)
    assert blocked.status_code == 201, blocked.content
    assert api(trainer).delete(f"/api/v1/blocks/{other.pk}/").status_code == 204


def test_a_trainer_coaching_anyone_is_told_why_the_account_stays(make_user, api):
    from tests.test_coaching_months import _accepted

    trainer, _client, _relation = _accepted(make_user, api, "s04_erase")
    refused = api(trainer).delete("/api/privacy/erase/?confirm=ERASE")
    assert refused.status_code == 409, refused.content
    body = refused.json()
    assert (body["code"], body["active_clients"]) == ("trainer_has_active_clients", 1)
