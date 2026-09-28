"""SIDE_BUGS S-242: a failed account email sent the live code and the address to Sentry.

`report_account_email_failure` logged with `exc_info` and captured the live exception.
Sentry records the local variables of every frame by default, and its scrubber does not
know `otp_code`, `plain_message` or `recipient_list`: the plaintext sign-up or reset
code, still good for ten minutes, and the recipient's address went to Sentry on every
failed send — so anyone reading the Sentry project during a mail outage could reset any
account. The notification emails report through the same function.

These start Sentry exactly as production does (`training_platform.observability.
start_sentry`: the Django and Celery integrations and the default ones, logging
included), with a transport that keeps what it would send.
"""
from __future__ import annotations

import json
import smtplib

import pytest

CODE = "482913"


@pytest.fixture
def production_sentry(monkeypatch):
    import sentry_sdk
    from sentry_sdk.transport import Transport

    from training_platform.observability import start_sentry

    events = []

    class _Keep(Transport):
        def capture_envelope(self, envelope):
            event = envelope.get_event()
            if event is not None:
                events.append(event)

    real_init = sentry_sdk.init

    def init(*args, **kwargs):
        kwargs["transport"] = _Keep()
        return real_init(*args, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", init)
    start_sentry("https://public@sentry.invalid/1", beat_schedule={}, monitored=set(),
                 environment="test", traces_sample_rate=0.0)
    try:
        yield events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)


def test_s242_a_failed_account_email_sends_sentry_neither_the_code_nor_the_address(
        make_user, monkeypatch, production_sentry):
    from users import utils

    user = make_user("s242_victim")

    def refused(*args, **kwargs):
        # A permanent failure: reported on the first attempt, while the code is live.
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 credentials not accepted")

    monkeypatch.setattr("users.utils.send_mail", refused)
    try:
        utils.send_password_reset_email(user, CODE)
    except smtplib.SMTPAuthenticationError as exc:
        utils.report_account_email_failure("password_reset", user.pk, exc, stage="send")
    else:
        pytest.fail("the send did not fail")

    sent = json.dumps(production_sentry, default=str)
    assert production_sentry, "the failure was not alerted"
    assert CODE not in sent, "the live code reached Sentry"
    assert user.email not in sent, "the recipient's address reached Sentry"
    # Still one alert, still saying what failed.
    assert len(production_sentry) == 1, sent
    assert production_sentry[0]["exception"]["values"][-1]["type"] == \
        "SMTPAuthenticationError"
    assert "535" in sent and "password_reset" in sent


def test_s242_production_sentry_records_no_local_variables(production_sentry):
    """Any other captured error: a frame's locals (a code, an address, a message body)
    never leave with it."""
    import sentry_sdk

    def holds_a_secret():
        otp_code = CODE  # noqa: F841 — a local, as `_send_code_email` holds one
        raise RuntimeError("boom")

    try:
        holds_a_secret()
    except RuntimeError as exc:
        sentry_sdk.capture_exception(exc)

    sent = json.dumps(production_sentry, default=str)
    assert production_sentry and "boom" in sent
    assert CODE not in sent
