"""Redis and email that work with whichever provider the owner picks (fx/prod-services).

The production-readiness audit of 2026-09-25 found four things between this backend and a
first deploy on any managed Redis or email provider:

1. **Redis per role.** Caches sat on DBs /0-/4 of one `REDIS_URL`, the channel layer on
   /5, the Celery broker on /6 and results on /7 — and the boot check refused a broker on
   DB0-DB5 of ANY host. Many managed Redis services serve database 0 only, so no broker
   there could ever boot. Each role now takes its own full URL (`REDIS_CACHE_URL`,
   `REDIS_CHANNELS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`), defaulting to the
   old layout on `REDIS_URL`; the boot check refuses a broker in the same Redis DATABASE
   as a cache or the channel layer, wherever that is.
2. **Email for any provider.** `smtp.gmail.com:587` was hard-coded, the From address was a
   placeholder domain and there was no timeout. Every part now comes from the environment.
3. **The account emails leave the request.** The sign-up and reset codes were sent inside
   the request, through SMTP, with no timeout: a stalled server held the request, and a
   failure was swallowed into `return False`. They go through a Celery task now, with
   retries, and a failure is logged and alerted (Sentry when configured).
4. **The saved language.** `preferred_language` defaulted to `en` and sign-up did not take
   it, so an Arabic user's first email — the sign-up code — arrived in English.

Production boots run in a clean subprocess, as in `tests/test_media_storage.py`:
importing `settings_production` mutates shared settings objects.
"""
import json
import logging
import os
import secrets
import smtplib
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from django.core import mail

ROOT = Path(__file__).resolve().parent.parent
JSON = "application/json"


# =============================================================================== helpers
#: A service-account key's SHAPE, as in tests/test_fx_media.py: the media check needs one
#: and these tests are about Redis and email, so it must not be what fails. A placeholder;
#: nothing parses it as key material at boot.
_GCS_KEY = json.dumps({
    "type": "service_account", "client_email": "media@example.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n",
})


def _prod_env(**overrides):
    """A production environment that passes every check of the gate; `None` removes a
    variable. Secrets are GENERATED or obvious placeholders, never key-shaped literals."""
    from cryptography.fernet import Fernet

    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_ALLOWED_HOSTS": "api.example.com",
        "DB_NAME": "tp", "DB_USER": "tp", "DB_PASSWORD": "x" * 20, "DB_HOST": "db.internal",
        "REDIS_URL": "redis://redis.internal:6379",
        "EMAIL_HOST": "smtp.mail-provider.test",
        "EMAIL_HOST_USER": "apikey", "EMAIL_HOST_PASSWORD": "y" * 12,
        "DEFAULT_FROM_EMAIL": "Yalla Gym <no-reply@yallagym.test>",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
        "DJANGO_SECRET_KEY": "s" * 64,
        "JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----",
        "JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----\nnot-a-key\n-----END PUBLIC KEY-----",
        "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "AI_TRAINING_PSEUDONYM_KEY": secrets.token_hex(32),
        "MEDIA_BACKEND": "gcs", "GS_BUCKET_NAME": "yalla-gym-media",
        "GS_CREDENTIALS_JSON": _GCS_KEY,
    }
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


_REPORT = """
import json
import training_platform.settings_production as s
s.enforce_production_safety()
print("BOOTED")
print(json.dumps({
    "caches": {alias: c["LOCATION"] for alias, c in s.CACHES.items()},
    "channels": s.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"],
    "broker": s.CELERY_BROKER_URL,
    "results": s.CELERY_RESULT_BACKEND,
    "email": {name: getattr(s, name) for name in (
        "EMAIL_BACKEND", "EMAIL_HOST", "EMAIL_PORT", "EMAIL_USE_TLS", "EMAIL_USE_SSL",
        "EMAIL_HOST_USER", "EMAIL_TIMEOUT", "DEFAULT_FROM_EMAIL", "SERVER_EMAIL")},
}))
"""


def _boot(**overrides):
    """Import settings_production in a clean process and run its safety gate."""
    return subprocess.run([sys.executable, "-c", _REPORT], cwd=str(ROOT),
                          env=_prod_env(**overrides), capture_output=True, text=True,
                          timeout=120)


def _booted(**overrides):
    """The settings a boot that must succeed ended up with."""
    result = _boot(**overrides)
    assert result.returncode == 0, result.stderr[-3000:]
    lines = result.stdout.strip().splitlines()
    assert "BOOTED" in lines, result.stdout
    return json.loads(lines[-1])


def _refused(*needles, **overrides):
    """The boot is refused, and its message names every one of `needles`."""
    result = _boot(**overrides)
    assert result.returncode != 0, f"booted: {result.stdout[-2000:]}"
    assert "BOOTED" not in result.stdout
    for needle in needles:
        assert needle in result.stderr, (needle, result.stderr[-3000:])
    return result.stderr


# ================================================================ 1. Redis, per role
def test_one_self_run_redis_keeps_todays_layout():
    """Nothing changes for a single Redis the owner runs: REDIS_URL alone gives the five
    caches /0-/4, the channel layer /5, the broker /6 and results /7. The broker used to
    be required separately; it now defaults to /6 like every other role."""
    got = _booted()
    host = "redis://redis.internal:6379"
    assert got["caches"] == {"default": f"{host}/0", "ratelimit": f"{host}/1",
                             "public": f"{host}/2", "private": f"{host}/3",
                             "edamam": f"{host}/4"}
    assert got["channels"] == [f"{host}/5"]
    assert (got["broker"], got["results"]) == (f"{host}/6", f"{host}/7")


def test_each_role_can_take_its_own_redis():
    """Four providers' worth of URLs, no REDIS_URL at all. The caches share one database
    (their key prefixes keep them apart), the channel layer may share it too, and the
    broker lives on a Redis of its own. TLS URLs pass through untouched."""
    cache = "rediss://default:cache-pass@cache.redis-provider.test:6379"
    channels = "rediss://default:cache-pass@cache.redis-provider.test:6379/0"
    broker = "rediss://default:queue-pass@queue.redis-provider.test:6380/0?ssl_cert_reqs=required"
    results = "rediss://default:queue-pass@queue.redis-provider.test:6380/1?ssl_cert_reqs=required"
    got = _booted(REDIS_URL=None, REDIS_CACHE_URL=cache, REDIS_CHANNELS_URL=channels,
                  CELERY_BROKER_URL=broker, CELERY_RESULT_BACKEND=results)
    assert set(got["caches"].values()) == {cache}
    assert got["caches"].keys() == {"default", "ratelimit", "public", "private", "edamam"}
    assert got["channels"] == [channels]
    assert (got["broker"], got["results"]) == (broker, results)


def test_a_redis_that_serves_only_database_zero_can_hold_the_broker():
    """The audit's case: a managed Redis that has database 0 and nothing else. The old
    check refused ANY broker URL ending in /0-/5, on any host, so none could boot."""
    got = _booted(CELERY_BROKER_URL="redis://queue.internal:6379/0",
                  CELERY_RESULT_BACKEND="redis://queue.internal:6379/0")
    assert got["broker"] == "redis://queue.internal:6379/0"
    assert got["caches"]["default"] == "redis://redis.internal:6379/0"


def test_a_broker_in_another_database_of_the_cache_redis_is_accepted():
    got = _booted(REDIS_URL=None, REDIS_CACHE_URL="redis://one.internal:6379/0",
                  REDIS_CHANNELS_URL="redis://one.internal:6379/0",
                  CELERY_BROKER_URL="redis://one.internal:6379/1",
                  CELERY_RESULT_BACKEND="redis://one.internal:6379/2")
    assert got["broker"] == "redis://one.internal:6379/1"


def test_a_broker_in_the_same_database_as_a_cache_is_refused():
    """A cache is flushed (django-redis `clear()` is FLUSHDB) and evicted under memory
    pressure; either deletes every queued job with it. The message says what the broker's
    Redis needs, and never prints a password."""
    # Today's layout, broker moved onto the private cache's database.
    stderr = _refused("CELERY_BROKER_URL", "'private' cache", "noeviction",
                      CELERY_BROKER_URL="redis://redis.internal:6379/3")
    assert "FLUSHDB" in stderr

    # Same instance and database, spelled differently: host case, default port, default
    # database and credentials do not make it another database.
    stderr = _refused("CELERY_BROKER_URL", "noeviction",
                      REDIS_URL=None,
                      REDIS_CACHE_URL="redis://one.internal:6379",
                      REDIS_CHANNELS_URL="redis://one.internal:6379/5",
                      CELERY_BROKER_URL="redis://queue-user:s3cret-pass@ONE.internal/0",
                      CELERY_RESULT_BACKEND="redis://one.internal:6379/7")
    assert "s3cret-pass" not in stderr


def test_a_broker_in_the_channel_layers_database_is_refused():
    _refused("CELERY_BROKER_URL", "channel layer",
             CELERY_BROKER_URL="redis://redis.internal:6379/5")


def test_a_role_with_no_redis_at_all_is_refused_by_name():
    """No REDIS_URL, and a role without its own URL: the message names both variables
    rather than guessing a localhost default."""
    _refused("REDIS_CHANNELS_URL", "REDIS_URL",
             REDIS_URL=None, REDIS_CACHE_URL="redis://cache.internal:6379",
             CELERY_BROKER_URL="redis://queue.internal:6379/0",
             CELERY_RESULT_BACKEND="redis://queue.internal:6379/1")


def test_a_redis_url_that_already_names_a_database_is_refused():
    """Every role appends its own database to REDIS_URL. One that already names a database
    was silently overridden (or produced `.../0/3`); it is refused and the operator is
    told to put databases in the role URLs."""
    _refused("REDIS_URL", REDIS_URL="redis://redis.internal:6379/2")


def test_localhost_is_refused_for_every_redis_role():
    _refused("REDIS_CACHE_URL", "localhost",
             REDIS_CACHE_URL="redis://localhost:6379")
    _refused("CELERY_RESULT_BACKEND", "localhost",
             CELERY_RESULT_BACKEND="redis://127.0.0.1:6379/7")
    # The channel layer's message is the one the gate already knows.
    _refused("WebSockets would be dead",
             REDIS_CHANNELS_URL="redis://localhost:6379/5")


def test_a_tls_broker_or_result_store_must_say_how_to_check_the_certificate():
    """Without `ssl_cert_reqs`, kombu connects to a rediss:// broker WITHOUT verifying its
    certificate (it logs a warning and uses CERT_NONE), and the Redis result backend
    raises on first use — inside the request that queued the task."""
    _refused("CELERY_BROKER_URL", "ssl_cert_reqs",
             CELERY_BROKER_URL="rediss://queue.internal:6379/0")
    _refused("CELERY_RESULT_BACKEND", "ssl_cert_reqs",
             CELERY_RESULT_BACKEND="rediss://queue.internal:6379/1")


def test_a_database_goes_into_the_url_path_not_after_its_options():
    from training_platform.redis_layout import endpoint, with_database

    assert with_database("rediss://u:p@h.test:6380?ssl_cert_reqs=required", 6) == \
        "rediss://u:p@h.test:6380/6?ssl_cert_reqs=required"
    assert with_database("redis://h.test:6379/", 2) == "redis://h.test:6379/2"
    # One keyspace, however it is spelled.
    assert endpoint("redis://H.test") == endpoint("rediss://user:pw@h.test:6379/0") \
        == ("h.test", 6379, 0)
    assert endpoint("redis://h.test:6379/1") != endpoint("redis://h.test:6379/0")
    assert endpoint("redis://h.test:6379?db=4") == ("h.test", 6379, 4)
    assert endpoint("amqp://h.test") is None


# ============================================================ 2. email, any provider
def test_email_comes_from_the_environment_for_any_provider():
    """STARTTLS on 587 unless told otherwise, a 10-second timeout, and the From address
    the owner set. The error mails of `mail_admins` leave from the same address."""
    got = _booted()["email"]
    assert got == {
        "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "EMAIL_HOST": "smtp.mail-provider.test", "EMAIL_PORT": 587,
        "EMAIL_USE_TLS": True, "EMAIL_USE_SSL": False, "EMAIL_HOST_USER": "apikey",
        "EMAIL_TIMEOUT": 10, "DEFAULT_FROM_EMAIL": "Yalla Gym <no-reply@yallagym.test>",
        "SERVER_EMAIL": "Yalla Gym <no-reply@yallagym.test>",
    }

    ssl = _booted(EMAIL_USE_SSL="True", EMAIL_TIMEOUT="7")["email"]
    assert (ssl["EMAIL_PORT"], ssl["EMAIL_USE_SSL"], ssl["EMAIL_USE_TLS"],
            ssl["EMAIL_TIMEOUT"]) == (465, True, False, 7)

    other = _booted(EMAIL_PORT="2525", SERVER_EMAIL="ops@yallagym.test")["email"]
    assert (other["EMAIL_PORT"], other["SERVER_EMAIL"]) == (2525, "ops@yallagym.test")

    source = (ROOT / "training_platform" / "settings_production.py").read_text()
    assert "smtp.gmail.com" not in source, "a provider is hard-coded again"


@pytest.mark.parametrize("overrides, needle", [
    ({"EMAIL_HOST": None}, "EMAIL_HOST"),
    ({"DEFAULT_FROM_EMAIL": None}, "DEFAULT_FROM_EMAIL"),
    # The placeholder settings_base ships: no provider will send for that domain.
    ({"DEFAULT_FROM_EMAIL": "noreply@trainingplatform.com"}, "DEFAULT_FROM_EMAIL"),
    ({"DEFAULT_FROM_EMAIL": "no-reply@example.com"}, "DEFAULT_FROM_EMAIL"),
    ({"DEFAULT_FROM_EMAIL": "Yalla Gym"}, "DEFAULT_FROM_EMAIL"),
    ({"EMAIL_USE_TLS": "True", "EMAIL_USE_SSL": "True"}, "EMAIL_USE_SSL"),
    ({"EMAIL_USE_TLS": "False", "EMAIL_USE_SSL": "False"}, "clear text"),
    ({"EMAIL_PORT": "465"}, "EMAIL_USE_SSL=True"),
    ({"EMAIL_USE_SSL": "True", "EMAIL_PORT": "587"}, "EMAIL_USE_TLS=True"),
    ({"EMAIL_TIMEOUT": "0"}, "EMAIL_TIMEOUT"),
])
def test_production_refuses_email_it_could_not_send(overrides, needle):
    _refused(needle, **overrides)


def test_development_and_tests_choose_their_email_backend_from_the_environment():
    """The console backend by default; `EMAIL_BACKEND` switches it (locmem for a test
    run, smtp against a local catcher)."""
    code = ("import training_platform.settings_local as s; "
            "print(s.EMAIL_BACKEND); print(s.EMAIL_TIMEOUT)")
    env = {k: v for k, v in os.environ.items() if k != "EMAIL_BACKEND"}
    plain = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, timeout=120)
    assert plain.returncode == 0, plain.stderr[-2000:]
    assert plain.stdout.split() == ["django.core.mail.backends.console.EmailBackend", "10"]

    env["EMAIL_BACKEND"] = "django.core.mail.backends.locmem.EmailBackend"
    switched = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=120)
    assert switched.stdout.split()[0] == "django.core.mail.backends.locmem.EmailBackend"


def test_a_stalled_smtp_server_fails_the_send_instead_of_holding_it(make_user, settings):
    """The server accepts the connection and never greets. With no timeout, smtplib
    waits for ever; the send must fail inside EMAIL_TIMEOUT and RAISE, so the task can
    retry it — it used to be swallowed into `return False`."""
    from users.utils import send_otp_email

    assert settings.EMAIL_TIMEOUT == 10, "no default SMTP timeout"

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()

    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST, settings.EMAIL_PORT = "127.0.0.1", listener.getsockname()[1]
    settings.EMAIL_USE_TLS = settings.EMAIL_USE_SSL = False
    settings.EMAIL_HOST_USER = settings.EMAIL_HOST_PASSWORD = ""
    settings.EMAIL_TIMEOUT = 1
    user = make_user("ps_stalled")
    outcome = {}

    def send():
        try:
            outcome["returned"] = send_otp_email(user, "123456")
        except Exception as exc:        # what the task sees
            outcome["raised"] = exc

    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    worker.join(8)
    try:
        assert not worker.is_alive(), "the send is still waiting on a silent server"
        assert isinstance(outcome.get("raised"), OSError), outcome
    finally:
        listener.close()
        for connection, _address in held:
            connection.close()


# ======================================================= 3. the emails leave the request
@pytest.fixture
def queued(monkeypatch):
    """What the request hands the broker, instead of the broker. The worker's side is
    run by hand with `.apply()` — exactly the task a worker would run."""
    from users.tasks import send_account_code_email

    calls = []

    def apply_async(args=None, kwargs=None, **options):
        calls.append({"args": tuple(args or ()), "kwargs": kwargs or {}, "options": options})

    monkeypatch.setattr(send_account_code_email, "apply_async", apply_async)
    return calls


def _run(call):
    from users.tasks import send_account_code_email

    return send_account_code_email.apply(args=call["args"], kwargs=call["kwargs"])


@pytest.fixture
def worker_retries(monkeypatch):
    """Let the eager executor RETRY the way a worker does.

    With `task_eager_propagates` (settings_base), Celery hands a task's `Retry` back to the
    caller instead of re-running it, so no retry ever ran in the suite. Off, the eager
    executor re-applies the task on `Retry` — the production retry path, minus the
    countdown — and records a final failure instead of raising it."""
    from users.tasks import send_account_code_email

    monkeypatch.setattr(send_account_code_email.app.conf, "task_eager_propagates", False)


@pytest.fixture
def users_log(caplog):
    """The `users` logger does not propagate (settings_base LOGGING), so caplog's root
    handler never sees it; hang the handler on it directly."""
    logger = logging.getLogger("users")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="users")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


@pytest.fixture
def sentry_events():
    """A real Sentry client with a transport that keeps what it would send."""
    import sentry_sdk
    from sentry_sdk.transport import Transport

    events = []

    class _Keep(Transport):
        def capture_envelope(self, envelope):
            event = envelope.get_event()
            if event is not None:
                events.append(event)

    sentry_sdk.init(dsn="https://public@sentry.invalid/1", transport=_Keep(),
                    default_integrations=False, auto_enabling_integrations=False)
    try:
        yield events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)


def _sign_up(api, username, **extra):
    headers = extra.pop("headers", {})
    return api().post("/api/auth/register/", {
        "username": username, "email": f"{username}@gate.test",
        "phone_number": "+963999999999", "password1": "Xx!23456long",
        "password2": "Xx!23456long", "user_type": "client",
        "date_of_birth": "1995-06-15", **extra,
    }, content_type=JSON, **headers)


def _code_in(message):
    import re

    found = re.search(r"\b(\d{6})\b", message.body)
    assert found, message.body
    return found.group(1)


def test_signing_up_queues_the_code_and_sends_nothing_itself(api, queued):
    """The request answers exactly as before and hands the worker the code SEALED: the
    broker (a Redis) never holds a usable sign-up code. The worker's email carries the
    code the row was hashed from, and that code verifies the account."""
    from users.models import CustomUser, OTPVerification
    from users.utils import _hash_otp, open_otp_code

    r = _sign_up(api, "ps_queue")
    assert r.status_code == 201, r.content
    assert set(r.json()) == {"user", "message", "requires_verification"}
    assert r.json()["requires_verification"] is True
    assert mail.outbox == [], "the request sent the email itself"

    assert len(queued) == 1
    user = CustomUser.objects.get(username="ps_queue")
    row = OTPVerification.objects.get(user=user, purpose="registration")
    otp_id, sealed = queued[0]["args"]
    assert otp_id == row.pk
    code = open_otp_code(sealed)
    assert code and _hash_otp(code) == row.otp_code
    assert code not in json.dumps(queued[0], default=str), "the plain code went to the broker"

    _run(queued[0])
    assert len(mail.outbox) == 1 and mail.outbox[0].to == [user.email]
    assert _code_in(mail.outbox[0]) == code
    verified = api().post("/api/auth/verify-otp/", {"email": user.email, "otp_code": code},
                          content_type=JSON)
    assert verified.status_code == 200, verified.content


def test_resend_and_password_reset_queue_their_codes_too(api, make_user, queued):
    pending = make_user("ps_resend", email_verified=False)
    r = api().post("/api/auth/resend-otp/", {"email": pending.email}, content_type=JSON)
    assert r.status_code == 200 and set(r.json()) == {"message"}

    member = make_user("ps_reset", email_verified=True)
    r = api().post("/api/auth/forgot-password/", {"email": member.email}, content_type=JSON)
    assert r.status_code == 200 and set(r.json()) == {"message", "email"}

    assert mail.outbox == [] and len(queued) == 2
    for call in queued:
        _run(call)
    assert [m.to for m in mail.outbox] == [[pending.email], [member.email]]


def test_a_code_replaced_by_a_newer_one_is_never_sent(api, queued):
    """A retry can run minutes late. By then the person may have asked again, and an
    email with the dead code would arrive after — and look like — the live one."""
    from users.models import CustomUser

    assert _sign_up(api, "ps_stale").status_code == 201
    user = CustomUser.objects.get(username="ps_stale")
    assert api().post("/api/auth/resend-otp/", {"email": user.email},
                      content_type=JSON).status_code == 200
    first, second = queued

    _run(first)
    assert mail.outbox == []
    _run(second)
    assert len(mail.outbox) == 1
    from users.utils import open_otp_code
    assert _code_in(mail.outbox[0]) == open_otp_code(second["args"][1])


def test_an_expired_code_is_never_sent(api, make_user, queued):
    from datetime import timedelta

    from django.utils import timezone

    from users.models import OTPVerification
    from users.utils import OTP_LIFETIME_MINUTES, _otp_seal, seal_otp_code

    assert _sign_up(api, "ps_expired").status_code == 201
    OTPVerification.objects.filter(user__username="ps_expired").update(
        expires_at=timezone.now() - timedelta(seconds=1))
    _run(queued[0])
    assert mail.outbox == []

    # A seal older than the code's lifetime does not open, whatever the row says: a
    # broker dump taken later holds nothing usable.
    otp_id, _sealed = queued[0]["args"]
    OTPVerification.objects.filter(pk=otp_id).update(
        expires_at=timezone.now() + timedelta(minutes=5))
    old = _otp_seal().encrypt_at_time(
        b"123456", int(timezone.now().timestamp()) - OTP_LIFETIME_MINUTES * 60 - 5).decode()
    _run({"args": (otp_id, old), "kwargs": {}})
    assert mail.outbox == []
    assert seal_otp_code("123456") != seal_otp_code("123456"), "a seal must not repeat"


def test_a_failed_send_is_retried_and_then_delivered(api, monkeypatch, worker_retries):
    from django.core.mail import send_mail as real_send_mail

    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise smtplib.SMTPServerDisconnected("connection dropped")
        return real_send_mail(*args, **kwargs)

    monkeypatch.setattr("users.utils.send_mail", flaky)
    assert _sign_up(api, "ps_flaky").status_code == 201
    assert len(attempts) == 2 and len(mail.outbox) == 1


def test_a_send_that_keeps_failing_is_logged_and_alerted_and_answers_as_before(
        api, make_user, monkeypatch, users_log, sentry_events, worker_retries):
    """Every attempt fails: the request answers exactly as it does today (201, 200, 200),
    and each failure is ONE error log line naming the account by id (never its address)
    and one Sentry event — never a silent `return False`."""
    from users.tasks import EMAIL_RETRY_COUNTDOWNS

    attempts = []

    def down(*args, **kwargs):
        attempts.append(1)
        raise ConnectionRefusedError("SMTP down")

    monkeypatch.setattr("users.utils.send_mail", down)

    r = _sign_up(api, "ps_down")
    assert r.status_code == 201 and set(r.json()) == {"user", "message", "requires_verification"}
    pending = make_user("ps_down_resend", email_verified=False)
    r = api().post("/api/auth/resend-otp/", {"email": pending.email}, content_type=JSON)
    assert r.status_code == 200 and set(r.json()) == {"message"}
    member = make_user("ps_down_reset", email_verified=True)
    r = api().post("/api/auth/forgot-password/", {"email": member.email}, content_type=JSON)
    assert r.status_code == 200

    assert len(attempts) == 3 * (1 + len(EMAIL_RETRY_COUNTDOWNS)), "not retried"
    reports = [rec for rec in users_log.records
               if rec.levelno >= logging.ERROR and "account email" in rec.getMessage()]
    assert len(reports) == 3, [r.getMessage() for r in reports]
    for record in reports:
        assert "@" not in record.getMessage(), record.getMessage()
    assert len(sentry_events) == 3
    for event in sentry_events:
        assert event["exception"]["values"][-1]["type"] == "ConnectionRefusedError"


def test_a_permanent_refusal_is_not_retried(api, monkeypatch, users_log, worker_retries):
    attempts = []

    def refused(*args, **kwargs):
        attempts.append(1)
        raise smtplib.SMTPRecipientsRefused({"x@gate.test": (550, b"no such mailbox")})

    monkeypatch.setattr("users.utils.send_mail", refused)
    assert _sign_up(api, "ps_refused").status_code == 201
    assert len(attempts) == 1
    assert any(rec.levelno >= logging.ERROR and "account email" in rec.getMessage()
               for rec in users_log.records)


def test_without_a_worker_a_failed_send_is_still_reported_once(api, monkeypatch,
                                                                users_log, sentry_events):
    """Development and the suite run tasks inline with `task_eager_propagates`, where
    Celery hands the retry back to the caller. The request still answers as before and
    the failure is reported once, with its real cause — not as a queueing error."""
    def down(*args, **kwargs):
        raise ConnectionRefusedError("SMTP down")

    monkeypatch.setattr("users.utils.send_mail", down)
    r = _sign_up(api, "ps_eager_down")
    assert r.status_code == 201 and set(r.json()) == {"user", "message", "requires_verification"}
    reports = [rec.getMessage() for rec in users_log.records
               if rec.levelno >= logging.ERROR and "account email" in rec.getMessage()]
    assert len(reports) == 1 and "stage=send" in reports[0], reports
    assert [e["exception"]["values"][-1]["type"] for e in sentry_events] == \
        ["ConnectionRefusedError"]


def test_an_unreachable_broker_does_not_fail_the_request(api, monkeypatch, users_log,
                                                         sentry_events):
    from kombu.exceptions import OperationalError

    from users.tasks import send_account_code_email

    def unreachable(*args, **kwargs):
        raise OperationalError("Error 111 connecting to queue.internal:6379")

    monkeypatch.setattr(send_account_code_email, "apply_async", unreachable)
    r = _sign_up(api, "ps_nobroker")
    assert r.status_code == 201 and set(r.json()) == {"user", "message", "requires_verification"}
    assert any(rec.levelno >= logging.ERROR and "account email" in rec.getMessage()
               for rec in users_log.records)
    assert len(sentry_events) == 1


# ======================================================== 4. the recipient's saved language
def test_a_sign_up_in_arabic_is_saved_in_arabic_and_its_code_arrives_in_arabic(api):
    """The app sends `Accept-Language: ar`; the sign-up keeps it, so the code — the
    first email the person ever gets — is Arabic, right to left, under the brand."""
    from users.models import CustomUser

    r = _sign_up(api, "ps_ar", headers={"HTTP_ACCEPT_LANGUAGE": "ar"})
    assert r.status_code == 201, r.content
    assert r.json()["user"]["preferred_language"] == "ar"
    assert CustomUser.objects.get(username="ps_ar").preferred_language == "ar"

    message = mail.outbox[-1]
    assert "يلا جيم" in message.subject and "تأكيد" in message.subject, message.subject
    assert "رمز" in message.body, message.body
    html = message.alternatives[0][0]
    assert 'dir="rtl"' in html and 'lang="ar"' in html


def test_the_language_named_at_sign_up_wins_over_the_header(api):
    from users.models import CustomUser

    r = _sign_up(api, "ps_en", preferred_language="en",
                 headers={"HTTP_ACCEPT_LANGUAGE": "ar"})
    assert r.status_code == 201, r.content
    assert CustomUser.objects.get(username="ps_en").preferred_language == "en"
    assert mail.outbox[-1].subject.endswith("Yalla Gym"), mail.outbox[-1].subject


def test_a_sign_up_that_names_no_language_is_saved_in_arabic(api):
    """Arabic is the platform's primary language; the column's `en` default is what made
    the first email English. A header naming a language we do not serve says nothing."""
    from users.models import CustomUser

    assert _sign_up(api, "ps_none").status_code == 201
    assert _sign_up(api, "ps_fr", headers={"HTTP_ACCEPT_LANGUAGE": "fr-FR"}).status_code == 201
    assert CustomUser.objects.get(username="ps_none").preferred_language == "ar"
    assert CustomUser.objects.get(username="ps_fr").preferred_language == "ar"


def test_a_language_we_do_not_serve_is_refused_with_its_code(api):
    r = _sign_up(api, "ps_bad_lang", preferred_language="fr")
    assert r.status_code == 400
    assert r.json()["field_errors"]["preferred_language"][0]["code"] == "invalid_choice"


def test_a_reset_code_goes_out_in_the_saved_language_not_the_requests(api, make_user):
    arabic = make_user("ps_reset_ar", preferred_language="ar")
    r = api().post("/api/auth/forgot-password/", {"email": arabic.email},
                   content_type=JSON, HTTP_ACCEPT_LANGUAGE="en")
    assert r.status_code == 200
    assert "يلا جيم" in mail.outbox[-1].subject, mail.outbox[-1].subject

    english = make_user("ps_reset_en", preferred_language="en")
    r = api().post("/api/auth/forgot-password/", {"email": english.email},
                   content_type=JSON, HTTP_ACCEPT_LANGUAGE="ar")
    assert r.status_code == 200
    assert mail.outbox[-1].subject.endswith("Yalla Gym"), mail.outbox[-1].subject


def test_the_language_is_read_when_the_email_is_sent(api, make_user, queued):
    """Saved, not remembered: a switch between the request and the worker counts."""
    user = make_user("ps_switch", preferred_language="ar", email_verified=False)
    assert api().post("/api/auth/resend-otp/", {"email": user.email},
                      content_type=JSON).status_code == 200
    type(user).objects.filter(pk=user.pk).update(preferred_language="en")
    _run(queued[0])
    assert mail.outbox[-1].subject.endswith("Yalla Gym"), mail.outbox[-1].subject


@pytest.mark.parametrize("language, brand, old", [
    ("en", "Yalla Gym", "Training Platform"), ("ar", "يلا جيم", "منصة التدريب")])
def test_both_emails_carry_the_brand_not_the_old_name(make_user, language, brand, old):
    from users.utils import send_otp_email, send_password_reset_email

    user = make_user(f"ps_brand_{language}", preferred_language=language)
    send_otp_email(user, "123456")
    send_password_reset_email(user, "654321")
    assert len(mail.outbox) == 2
    for message in mail.outbox:
        html = message.alternatives[0][0]
        assert brand in message.subject and brand in html, (message.subject, language)
        assert old not in message.subject + message.body + html
