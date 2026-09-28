"""Sentry: error reporting, and cron monitors for the beat jobs that move money.

Optional — it starts only when SENTRY_DSN is set (settings_production.py), and the boot
check says loudly when it is not. What it adds over the log stream is an ALERT: a log
line is only read when someone goes looking, and nobody looks at a machine that seems
fine. The audit (2026-09-25) found errors reached neither: logs were on files a stopped
machine loses, and `mail_admins` sent to an ADMINS list set nowhere.

Cron monitors. `CeleryIntegration(monitor_beat_tasks=True)` makes beat check in with
Sentry when it sends an entry, and the worker check in again when the task finishes or
fails, so Sentry alerts when a job is missed, fails, or runs too long — even when the
process that should run it is dead, which is exactly when nothing else can say so.
Monitors are billed per monitor, so only the watched money jobs get one
(`settings.BACKGROUND_WATCHED_JOBS`); every other beat entry is excluded by name. The
monitor's slug is the beat entry's name. Both beat and the worker import these
settings, so both processes carry the integration, which the check-ins need.

Privacy: `send_default_pii=False` keeps user fields and request bodies out, and
`before_send` / `before_breadcrumb` mask email addresses in message text and exception
values, which that flag does not cover (`training_platform/log_privacy.py`).
"""
import re

from training_platform.log_privacy import redact_emails


def excluded_beat_entries(beat_schedule, monitored):
    """Regexes (anchored, one per entry) for every beat entry that is NOT monitored."""
    return [f"^{re.escape(name)}$" for name in beat_schedule if name not in monitored]


def _redact_key(mapping, key):
    if isinstance(mapping, dict) and isinstance(mapping.get(key), str):
        mapping[key] = redact_emails(mapping[key])


def _scrub_event(event, _hint):
    logentry = event.get("logentry")
    _redact_key(logentry, "message")
    _redact_key(logentry, "formatted")
    if isinstance(logentry, dict) and isinstance(logentry.get("params"), (list, tuple)):
        logentry["params"] = [redact_emails(p) for p in logentry["params"]]
    _redact_key(event, "message")
    for exc in ((event.get("exception") or {}).get("values") or []):
        _redact_key(exc, "value")
    for crumb in ((event.get("breadcrumbs") or {}).get("values") or []):
        _redact_key(crumb, "message")
    return event


def _scrub_breadcrumb(crumb, _hint):
    _redact_key(crumb, "message")
    return crumb


def start_sentry(dsn, *, beat_schedule, monitored, environment, traces_sample_rate):
    """`sentry_sdk.init` with the project's integrations. Raises what Sentry raises
    (a malformed DSN is `BadDsn`); the caller turns that into a refused boot."""
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            DjangoIntegration(),
            CeleryIntegration(
                monitor_beat_tasks=True,
                exclude_beat_tasks=excluded_beat_entries(beat_schedule, monitored),
            ),
        ],
        traces_sample_rate=traces_sample_rate,
        environment=environment,
        # Never ship PII (emails, request bodies) to a third party.
        send_default_pii=False,
        before_send=_scrub_event,
        before_breadcrumb=_scrub_breadcrumb,
    )
