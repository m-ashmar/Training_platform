"""The platform and infrastructure items of the production-readiness audit (2026-09-25).

One group per item. Each test fails against b204be6, the code the audit read.

| # | Item | Tests |
|---|---|---|
| 1 | `fly.toml` runs web, worker and beat as separate process groups | `test_fly_*processes*`, `test_fly_worker_*` |
| 2 | a beat heartbeat, a public background health route, Sentry crons for the money jobs | `test_heartbeat_*`, `test_background_health_*`, `test_sentry_cron_*` |
| 3 | the platform's health probe is answered whatever Host it sends, and nothing else is | `test_the_health_probe_*` |
| 4 | no volume, the upload spool on /tmp, one machine always up, room for sockets | `test_fly_capacity_*`, `test_the_image_*` |
| 5 | production logs are one JSON line each on stdout, with no email address in them | `test_production_logs_*`, `test_mask_email_*`, `test_no_log_call_*`, `test_the_otp_flow_*` |
| 6 | database connections under Daphne: none kept unless a pooler is declared | `test_database_*` |
| 7 | every recorded or checked client IP is the trusted one, not the proxy's | `test_client_ip_*`, `test_the_agent_allowlist_*` |
| 8 | the build context and the committed SQLite file | `test_the_build_context_*`, `test_the_dev_sqlite_*` |
| 9 | CI runs the suite on the working branch | `test_ci_*` |
| 10 | Sentry stays optional, and its absence is said out loud at boot | `test_sentry_*` |

Production settings are exercised in a clean subprocess, the way `tests/test_fx_media.py`
does it: importing `settings_production` mutates shared settings objects, and the boot
check has to see a whole, real environment. Nothing in them reaches the network.
"""
import ast
import json
import os
import re
import secrets
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
JSON = "application/json"

#: The four beat entries that move money (the audit's deploy finding 4): the poller is
#: the only thing that completes a payment in reconcile mode, renewals charge coaching
#: months, the payout backstop pays trainers, and the sweep ends lapsed plans.
MONEY_ENTRIES = {"poll-open-payments", "renew-coaching-periods", "settle-owed-payouts",
                 "expire-lapsed-subscriptions"}


def _fly():
    return tomllib.loads((ROOT / "fly.toml").read_text())


def _vm_for(fly, group):
    return next((vm for vm in fly.get("vm", []) if group in vm.get("processes", [])), None)


# ------------------------------------------------------------------ production subprocess
def _prod_env(**overrides):
    """A production environment that passes every other check of the boot gate."""
    from cryptography.fernet import Fernet

    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_ALLOWED_HOSTS": "api.example.com",
        "DB_NAME": "tp", "DB_USER": "tp", "DB_PASSWORD": "x" * 20, "DB_HOST": "db.internal",
        "REDIS_URL": "redis://redis.internal:6379",
        "CELERY_BROKER_URL": "redis://redis.internal:6379/6",
        "EMAIL_HOST_USER": "a@example.com", "EMAIL_HOST_PASSWORD": "y" * 12,
        # Deliberate contract change (fx/prod-services, merged first): production no
        # longer hard-codes Gmail, so the SMTP host and the From address are required
        # from the environment (tests/test_prod_services.py), as in
        # tests/test_media_storage.py's environment.
        "EMAIL_HOST": "smtp.mail-provider.test",
        "DEFAULT_FROM_EMAIL": "Yalla Gym <no-reply@yallagym.test>",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "CSRF_TRUSTED_ORIGINS": "https://app.example.com",
        "DJANGO_SECRET_KEY": "s" * 64,
        # Placeholders: settings only reads them into SIMPLE_JWT, nothing parses them.
        "JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----",
        "JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----\nnot-a-key\n-----END PUBLIC KEY-----",
        # Generated, never a literal: a key-shaped string in the repository is what
        # detect-secrets exists to catch.
        "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "AI_TRAINING_PSEUDONYM_KEY": secrets.token_hex(32),
        "MEDIA_BACKEND": "gcs", "GS_BUCKET_NAME": "yalla-gym-media",
        "GS_CREDENTIALS_JSON": json.dumps({
            "type": "service_account",
            "client_email": "media@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n",
        }),
        # Deliberate contract change (fx/w1-privacy, BACKEND_BUGS B-174, the owner's
        # decision of 2026-09-28): production refuses to boot without an https base for
        # the public pages, so this environment, which passes every other check,
        # carries one.
        "PUBLIC_PAGES_BASE_URL": "https://yallagym.example",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def _run_prod(code, **overrides):
    """Run `code` after importing settings_production as `s` in a clean process."""
    prelude = "import training_platform.settings_production as s\n"
    return subprocess.run([sys.executable, "-c", prelude + code], cwd=str(ROOT),
                          env=_prod_env(**overrides), capture_output=True, text=True,
                          timeout=180)


def _json_lines(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out.append(json.loads(line))
    return out


# =========================================================================== 1. processes
def test_fly_runs_web_worker_and_beat_as_separate_process_groups():
    """The worker ran `--beat` inside itself, so scaling the worker to two machines ran
    the money jobs twice, and one OOM stopped both the queue and the clock."""
    fly = _fly()
    processes = fly["processes"]
    assert set(processes) == {"web", "worker", "beat"}, processes

    worker, beat = processes["worker"].split(), processes["beat"].split()
    assert "--beat" not in worker and "-B" not in worker, processes["worker"]
    assert worker[:4] == ["celery", "-A", "training_platform", "worker"], worker
    assert beat[:4] == ["celery", "-A", "training_platform", "beat"], beat
    assert "migrate" not in processes["web"]
    # `release_command` belongs in [deploy]. Written below [processes], TOML made it a
    # key OF that table: a process group called "release_command" that runs `migrate`
    # as a long-lived machine, and no release command at all.
    assert "migrate" in fly["deploy"]["release_command"]

    for group in ("web", "worker", "beat"):
        assert _vm_for(fly, group), f"no [[vm]] sizes the {group} group"
    # Beat is one machine and only one; the file says so where an operator will read it.
    text = (ROOT / "fly.toml").read_text()
    assert re.search(r"beat.{0,80}exactly one machine", text, re.I | re.S), \
        "fly.toml does not say beat runs on exactly one machine"


def test_fly_worker_concurrency_fits_its_512_mb_machine():
    """Two prefork children beside the parent is what 512 MB holds with an image decode
    (128 MB budget) in one of them, and a child that grows is replaced between tasks."""
    fly = _fly()
    worker = fly["processes"]["worker"]
    assert "--concurrency=2" in worker, worker
    assert re.search(r"--max-memory-per-child=\d+", worker), worker
    assert _vm_for(fly, "worker")["memory"] == "512mb"


# ============================================================================ 2. heartbeat
@pytest.fixture
def background(settings):
    """The background health state, forgotten, with tasks treated as queued (not inline)."""
    from training_platform import background_health as bh

    settings.CELERY_TASK_ALWAYS_EAGER = False
    bh.forget_all()
    yield bh
    bh.forget_all()


def test_heartbeat_is_scheduled_every_minute_and_registered():
    from celery.schedules import crontab
    from django.conf import settings

    from training_platform.celery import app

    entries = [e for e in settings.CELERY_BEAT_SCHEDULE.values()
               if e["task"] == "training_platform.background_heartbeat"]
    assert len(entries) == 1, "the heartbeat is not on the beat schedule"
    schedule = entries[0]["schedule"]
    assert isinstance(schedule, crontab) and schedule._orig_minute == "*"

    app.loader.import_default_modules()
    assert "training_platform.background_heartbeat" in app.tasks


def test_every_watched_job_is_a_real_beat_entry_and_the_money_jobs_are_watched():
    from django.conf import settings

    watched = settings.BACKGROUND_WATCHED_JOBS
    assert MONEY_ENTRIES <= set(watched), watched
    for name, max_age in watched.items():
        assert name in settings.CELERY_BEAT_SCHEDULE, name
        assert max_age >= 120, (name, max_age)


@pytest.mark.django_db
def test_background_health_is_ok_after_a_heartbeat(api, background):
    background.background_heartbeat.apply()

    resp = api().get("/api/auth/health/background/")
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["status"] == "ok"
    assert body["heartbeat"]["status"] == "ok"
    assert 0 <= body["heartbeat"]["age_seconds"] <= 5
    # Jobs that have not run yet are waiting, not stale: the clock has only just started.
    for name in MONEY_ENTRIES:
        assert body["jobs"][name]["status"] == "waiting", body["jobs"][name]


@pytest.mark.django_db
def test_background_health_is_503_when_the_heartbeat_is_missing_or_old(api, background):
    missing = api().get("/api/auth/health/background/")
    assert missing.status_code == 503, missing.content
    assert missing.json()["status"] == "stale"
    assert missing.json()["heartbeat"]["status"] == "missing"

    from django.conf import settings

    old = time.time() - settings.BACKGROUND_HEARTBEAT_MAX_AGE_SECONDS - 5
    background.ops_cache().set(background.HEARTBEAT_KEY, old, timeout=None)
    stale = api().get("/api/auth/health/background/")
    assert stale.status_code == 503
    assert stale.json()["heartbeat"]["status"] == "stale"


@pytest.mark.django_db
def test_background_health_follows_the_money_jobs(api, background):
    """A heartbeat proves the clock and the queue. It does not prove the poller still
    finishes: a job that raises every minute leaves the heartbeat perfectly healthy."""
    from subscription.tasks import poll_open_payments

    background.background_heartbeat.apply()
    poll_open_payments.apply()          # a real run, recorded by the success signal

    body = api().get("/api/auth/health/background/").json()
    assert body["jobs"]["poll-open-payments"]["status"] == "ok", body

    from django.conf import settings

    limit = settings.BACKGROUND_WATCHED_JOBS["poll-open-payments"]
    background.ops_cache().set(background.SUCCESS_KEY.format("poll-open-payments"),
                               time.time() - limit - 5, timeout=None)
    resp = api().get("/api/auth/health/background/")
    assert resp.status_code == 503
    assert resp.json()["jobs"]["poll-open-payments"]["status"] == "stale"


@pytest.mark.django_db
def test_background_health_a_job_that_never_runs_goes_stale(api, background):
    """"Waiting" cannot last for ever, or a job that is never registered would never
    alert: the heartbeat starts a watch clock for each job, once."""
    from django.conf import settings

    background.background_heartbeat.apply()
    limit = settings.BACKGROUND_WATCHED_JOBS["renew-coaching-periods"]
    background.ops_cache().set(background.WATCH_KEY.format("renew-coaching-periods"),
                               time.time() - limit - 5, timeout=None)
    background.background_heartbeat.apply()      # must not restart the clock

    resp = api().get("/api/auth/health/background/")
    assert resp.status_code == 503
    assert resp.json()["jobs"]["renew-coaching-periods"]["status"] == "stale"


@pytest.mark.django_db
def test_background_health_when_its_store_is_down(api, background, monkeypatch):
    class Down:
        def get_many(self, *_a, **_k):
            raise ConnectionError("redis is gone")

    # A context, so the real store is back before the fixture's own teardown uses it.
    with monkeypatch.context() as patch:
        patch.setattr(background, "ops_cache", lambda: Down())
        resp = api().get("/api/auth/health/background/")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unreachable"


@pytest.mark.django_db
def test_background_health_in_inline_mode_has_nothing_to_watch(api, settings):
    """Development and the test suite run every task inline: there is no worker or beat
    whose silence could mean anything."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    resp = api().get("/api/auth/health/background/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "inline"


@pytest.mark.django_db
def test_heartbeat_the_web_health_check_never_depends_on_the_background(api, background):
    """Fly restarts a web machine whose check fails. A late beat must never do that."""
    resp = api().get("/api/auth/health/")
    assert resp.status_code == 200, resp.content
    assert "heartbeat" not in resp.json()

    fly = _fly()
    paths = [c["path"] for c in fly["http_service"]["checks"]]
    assert paths == ["/api/auth/health/"], paths


def test_heartbeat_route_is_in_the_contract():
    text = (ROOT / "API_CONTRACT.md").read_text()
    assert re.search(r"^\| `GET` \| `/api/auth/health/background/` \| public \| "
                     r"`BackgroundHealthView` \|", text, re.M)


# ================================================================ 3. health probe vs host
@pytest.mark.django_db
def test_the_health_probe_is_answered_whatever_host_it_sends(settings):
    """Fly's checker may send the machine's private address as Host. ALLOWED_HOSTS
    refuses wildcards in production, so every probe answered 400 DisallowedHost and a
    deploy could never be marked healthy."""
    from django.test import Client

    settings.ALLOWED_HOSTS = ["api.example.com"]
    probe = Client(HTTP_HOST="172.19.3.4:8000")

    ok = probe.get("/api/auth/health/")
    assert ok.status_code == 200, ok.content
    assert ok.json()["database"] == "ok"
    assert probe.head("/api/auth/health/").status_code == 200

    # Exactly that path and those methods. Everything else still refuses the host.
    assert probe.get("/api/auth/trainers/public/").status_code == 400
    assert probe.get("/api/auth/health/background/").status_code == 400
    assert probe.post("/api/auth/health/").status_code == 400
    assert probe.get("/api/auth/health").status_code == 400

    # And an allowed host is served exactly as before.
    assert Client(HTTP_HOST="api.example.com").get("/api/auth/health/").status_code == 200


# ============================================================================ 4. capacity
def test_fly_capacity_no_volume_and_the_upload_spool_on_tmp():
    """A volume attaches to one machine, and one volume existed, so there could only
    ever be one web machine. Media lives in object storage in production; the volume
    held nothing but the multipart spool."""
    fly = _fly()
    assert "mounts" not in fly, "a [mounts] volume pins the web group to one machine"
    env = fly["env"]
    assert env["FILE_UPLOAD_TEMP_DIR"].startswith("/tmp/"), env
    assert not env.get("MEDIA_ROOT", "").startswith("/data"), env
    assert "/data" not in (ROOT / "Dockerfile").read_text()
    assert "/data" not in (ROOT / "docker-entrypoint.sh").read_text()


def test_fly_capacity_one_machine_stays_up_and_sockets_have_room():
    """`min_machines_running = 0` cold-started Daphne on the first request after idle,
    and 15 connections per machine — WebSockets included — served the whole platform."""
    service = _fly()["http_service"]
    assert service["min_machines_running"] >= 1
    concurrency = service["concurrency"]
    assert concurrency["type"] == "connections"
    assert concurrency["hard_limit"] >= 100, concurrency
    assert concurrency["soft_limit"] < concurrency["hard_limit"]


def test_the_image_entrypoint_makes_the_spool_and_still_drops_privileges():
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text()
    assert "FILE_UPLOAD_TEMP_DIR" in entrypoint
    assert "setpriv" in entrypoint and 'exec "$@"' in entrypoint


# ============================================================================= 5. logging
_LOG_PROBE = r'''
import logging, logging.config
logging.config.dictConfig(s.LOGGING)
logging.getLogger("subscription.tasks").error("poller failed on a checkout")
logging.getLogger("wallet.views").warning("refused top-up for someone.real@example.com")
try:
    1 / 0
except ZeroDivisionError:
    logging.getLogger("payouts.services").exception("settle failed\nsecond line")
logging.getLogger("django.request").error("Internal Server Error: /api/x/")
logging.getLogger("celery.task").info("task ran")
logging.getLogger("users").info("user 5 verified")
logging.getLogger("django.db.backends").debug("SELECT 1")
'''


def test_production_logs_are_one_json_line_each_on_stdout():
    """Every app logger wrote to rotating files on the container's own disk, which a
    stopped machine loses; the only stdout handler was DEBUG-only; wallet, payouts,
    policy and chat had no configuration at all."""
    proc = _run_prod(_LOG_PROBE)
    assert proc.returncode == 0, proc.stderr[-2000:]

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]    # every line is one JSON object
    by_logger = {r["logger"]: r for r in records}
    assert set(by_logger) == {"subscription.tasks", "wallet.views", "payouts.services",
                              "django.request", "celery.task", "users"}, by_logger
    for record in records:
        assert {"ts", "level", "logger", "msg"} <= set(record), record

    failed = by_logger["payouts.services"]
    assert failed["level"] == "ERROR"
    assert "ZeroDivisionError" in failed["exc"] and "second line" in failed["msg"]

    # The address never reaches the log stream, whoever logged it.
    assert "someone.real@example.com" not in proc.stdout
    assert "s***@example.com" in by_logger["wallet.views"]["msg"]


def test_production_logs_have_no_files_and_no_mail_only_error_path():
    code = "import json; print(json.dumps(s.LOGGING, default=str))"
    proc = _run_prod(code)
    assert proc.returncode == 0, proc.stderr[-2000:]
    config = json.loads(proc.stdout.strip().splitlines()[-1])

    classes = {h.get("class", "") for h in config["handlers"].values()}
    assert not any("FileHandler" in c for c in classes), classes
    assert not any("AdminEmailHandler" in c for c in classes), classes
    assert config["root"]["handlers"], "nothing catches an unconfigured logger"


def test_mask_email_keeps_the_domain_and_one_letter():
    from training_platform.log_privacy import mask_email, redact_emails

    assert mask_email("someone.real@example.com") == "s***@example.com"
    assert mask_email("") == "***" and mask_email(None) == "***"
    assert mask_email("not an address") == "***"
    text = "to a.b@x.org and c@y.co, then again a.b@x.org"
    assert redact_emails(text) == "to a***@x.org and c***@y.co, then again a***@x.org"
    assert redact_emails(redact_emails(text)) == redact_emails(text)


_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}


def _is_address_name(name):
    """`email`, `user.email`, `to_email`, `emails` — not `email_verified`, a boolean."""
    return name.lower().endswith(("email", "emails"))


def _unmasked_email_refs(node):
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in (
            "mask_email", "redact_emails"):
        return []
    hits = []
    if isinstance(node, ast.Name) and _is_address_name(node.id):
        hits.append(node.id)
    elif isinstance(node, ast.Attribute) and _is_address_name(node.attr):
        hits.append(node.attr)
    for child in ast.iter_child_nodes(node):
        hits += _unmasked_email_refs(child)
    return hits


def test_no_log_call_passes_an_email_address_unmasked():
    """Sixteen log lines put the address into the message, and Sentry's
    `send_default_pii=False` does not scrub message text."""
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=str(ROOT), capture_output=True,
                           text=True, check=True).stdout.split()
    offenders = []
    for rel in files:
        if rel.startswith(("tests/", "_excluded/", "mobile/", "flutter/")) \
                or "/migrations/" in rel:
            continue
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _LOG_METHODS):
                continue
            owner = node.func.value
            owner_name = getattr(owner, "id", None) or getattr(owner, "attr", "")
            if "log" not in owner_name.lower():
                continue
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if _unmasked_email_refs(arg):
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, offenders


@pytest.mark.django_db
def test_the_otp_flow_logs_no_email_address(make_user, caplog):
    import logging

    from users.utils import create_otp, create_password_reset_otp, verify_otp

    user = make_user("pp_otp_log")
    users_logger = logging.getLogger("users")
    users_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO):
            create_otp(user)
            verify_otp(user.email, "000000")
            create_password_reset_otp(user)
    finally:
        users_logger.removeHandler(caplog.handler)

    messages = [r.getMessage() for r in caplog.records]
    assert messages, "nothing was logged, so nothing was checked"
    assert not [m for m in messages if user.email in m], messages


# ============================================================================ 6. database
_DB_PROBE = ("import json; d = s.DATABASES['default']; "
             "print(json.dumps({k: d.get(k) for k in ('CONN_MAX_AGE', 'CONN_HEALTH_CHECKS', "
             "'DISABLE_SERVER_SIDE_CURSORS', 'OPTIONS')}))\n"
             "s.enforce_production_safety(); print('BOOTED')\n")


def _db(**overrides):
    proc = _run_prod(_DB_PROBE, **overrides)
    config = json.loads(next(line for line in proc.stdout.splitlines()
                             if line.startswith("{")))
    return proc, config


def test_database_connections_are_not_kept_under_daphne_by_default():
    """Under ASGI every request's sync code runs on a thread of its own, so a kept
    connection is never reused: CONN_MAX_AGE=600 idled connections against
    max_connections until the garbage collector found them."""
    proc, config = _db()
    assert proc.returncode == 0 and "BOOTED" in proc.stdout, proc.stderr[-2000:]
    assert config["CONN_MAX_AGE"] == 0
    assert config["CONN_HEALTH_CHECKS"] is True
    assert not config["DISABLE_SERVER_SIDE_CURSORS"]


def test_database_keeping_connections_needs_a_declared_pooler():
    refused, config = _db(DB_CONN_MAX_AGE="60")
    assert refused.returncode != 0 and "BOOTED" not in refused.stdout
    assert "DB_CONN_MAX_AGE" in refused.stderr and "DB_POOLER" in refused.stderr

    pooled, config = _db(DB_CONN_MAX_AGE="60", DB_POOLER="transaction")
    assert pooled.returncode == 0, pooled.stderr[-2000:]
    assert config["CONN_MAX_AGE"] == 60
    # A transaction pooler hands each transaction a different server connection, and a
    # server-side cursor lives on the connection that opened it.
    assert config["DISABLE_SERVER_SIDE_CURSORS"] is True

    unknown, _ = _db(DB_POOLER="pgcat-magic")
    assert unknown.returncode != 0 and "DB_POOLER" in unknown.stderr


def test_database_tls_mode_is_configurable_and_checked():
    proc, config = _db(DB_SSLMODE="require")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert config["OPTIONS"]["sslmode"] == "require"

    bad, _ = _db(DB_SSLMODE="sometimes")
    assert bad.returncode != 0 and "DB_SSLMODE" in bad.stderr


# =========================================================================== 7. client IP
def _request(xff=None, remote="172.16.0.9"):
    from django.test import RequestFactory

    extra = {"REMOTE_ADDR": remote}
    if xff is not None:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **extra)


def test_client_ip_the_charging_throttle_keys_on_the_trusted_ip():
    """DRF's own `get_ident` with no NUM_PROXIES keys on the WHOLE X-Forwarded-For, so a
    caller rotated the left-hand entries and got a fresh bucket every request."""
    from wallet.throttles import ChargingRateThrottle

    throttle = ChargingRateThrottle()
    assert throttle.get_ident(_request("1.1.1.1, 203.0.113.7")) == "203.0.113.7"
    assert throttle.get_ident(_request("9.9.9.9, 203.0.113.7")) == "203.0.113.7"


def test_client_ip_the_account_adapter_uses_the_trusted_ip():
    """allauth's adapter took the LEFT-most X-Forwarded-For entry, which the client
    writes."""
    from allauth.account.adapter import get_adapter

    assert get_adapter().get_client_ip(_request("6.6.6.6, 203.0.113.7")) == "203.0.113.7"


def test_client_ip_a_recorded_ip_is_an_address_or_nothing():
    from training_platform.middleware import get_recordable_client_ip

    assert get_recordable_client_ip(_request("203.0.113.7")) == "203.0.113.7"
    assert get_recordable_client_ip(_request(None, remote="10.1.2.3")) == "10.1.2.3"
    assert get_recordable_client_ip(_request("not-an-ip")) is None


def test_client_ip_no_app_code_reads_the_raw_headers_itself():
    """REMOTE_ADDR behind Fly is the proxy. One helper decides the client address."""
    files = subprocess.run(["git", "ls-files", "*.py"], cwd=str(ROOT), capture_output=True,
                           text=True, check=True).stdout.split()
    readers = []
    for rel in files:
        if rel.startswith(("tests/", "_excluded/", "mobile/", "flutter/")) \
                or "/migrations/" in rel or rel == "training_platform/middleware.py":
            continue
        source = (ROOT / rel).read_text(encoding="utf-8")
        if re.search(r"['\"](REMOTE_ADDR|HTTP_X_FORWARDED_FOR)['\"]", source):
            readers.append(rel)
    assert not readers, readers


@pytest.mark.django_db
def test_client_ip_analytics_sessions_record_the_client_not_the_proxy(make_user, api):
    from analytics.models import UserSession

    user = make_user("pp_ip_session")
    resp = api(user).post("/api/analytics/sessions/", {"session_id": "pp-ip-1"},
                          content_type=JSON, REMOTE_ADDR="172.16.0.9",
                          HTTP_X_FORWARDED_FOR="203.0.113.7")
    assert resp.status_code == 201, resp.content
    assert UserSession.objects.get(session_id="pp-ip-1").ip_address == "203.0.113.7"

    garbage = api(user).post("/api/analytics/sessions/", {"session_id": "pp-ip-2"},
                             content_type=JSON, HTTP_X_FORWARDED_FOR="not-an-ip")
    assert garbage.status_code == 201, garbage.content
    assert UserSession.objects.get(session_id="pp-ip-2").ip_address is None


def _signed_topup(api, agent, key_id, secret, client, key, **extra):
    from wallet.security import compute_hmac_signature

    ts = int(time.time())
    ident, amount, currency = str(client.id), "100.00", "SYP"
    sig = compute_hmac_signature(secret, f"{ident}|{amount}|{ts}|{key}|{currency}")
    return api(agent).post(
        "/api/wallet/agent/topup/",
        {"client_identifier": ident, "amount": amount, "currency": currency,
         "idempotency_key": key, "timestamp": ts, "signature": sig},
        content_type=JSON,
        HTTP_X_AGENT_AUTH=f"AgentAuth key_id={key_id},signature={sig},timestamp={ts}",
        **extra)


@pytest.mark.django_db
def test_the_agent_allowlist_checks_the_client_behind_the_proxy(make_user, api):
    """Every agent with an allowlist was refused on every top-up, because the check read
    the proxy's address; and every wallet audit row recorded the proxy."""
    from decimal import Decimal

    from wallet.models import AgentAPIKey, WalletAuditLog, ensure_agent_profile
    from wallet.security import encrypt_secret

    agent = make_user("pp_ip_agent", user_type="agent")
    profile = ensure_agent_profile(agent)
    profile.status = "active"
    profile.daily_limit = profile.monthly_limit = Decimal("100000")
    profile.ip_allowlist = ["203.0.113.7"]
    profile.save()
    secret = "s" * 64
    AgentAPIKey.objects.create(agent=profile, key_id="ppipkey", hashed_key="x",
                               secret_ciphertext=encrypt_secret(secret), is_active=True)
    client = make_user("pp_ip_client")

    through_proxy = _signed_topup(api, agent, "ppipkey", secret, client, "pp-ip-a",
                                  REMOTE_ADDR="172.16.0.9",
                                  HTTP_X_FORWARDED_FOR="203.0.113.7")
    assert through_proxy.status_code == 200, through_proxy.content
    rows = WalletAuditLog.objects.filter(actor=agent).exclude(ip_address=None)
    assert rows.exists()
    assert set(rows.values_list("ip_address", flat=True)) == {"203.0.113.7"}

    # A client writing the allowed address on the LEFT is still the address on the right.
    spoofed = _signed_topup(api, agent, "ppipkey", secret, client, "pp-ip-b",
                            REMOTE_ADDR="172.16.0.9",
                            HTTP_X_FORWARDED_FOR="203.0.113.7, 198.51.100.4")
    assert spoofed.status_code == 403, spoofed.content
    assert spoofed.json()["code"] == "agent_ip_not_allowed"
    denied = WalletAuditLog.objects.filter(event_type="wallet.topup.denied_ip").latest("id")
    assert denied.ip_address == "198.51.100.4"


# ==================================================================== 8. build context
def test_the_build_context_leaves_out_the_clients_and_dev_state():
    """`yalla_gym/` (1.2 GB) and the two abandoned clients (~2.4 GB) went into every
    build context, and `*.log` did not match the rotated dev logs, which hold emails."""
    patterns = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
                if line.strip() and not line.startswith("#")}
    for required in ("yalla_gym/", "mobile/", "flutter/", "_excluded/", "logs/", "media/",
                     "staticfiles/", "db.sqlite3", ".claude/", "tests/", "docs/",
                     "**/*.log", "**/*.log.*", "**/__pycache__/"):
        assert required in patterns, f"{required} would be sent into the image"


def test_the_dev_sqlite_is_not_tracked():
    tracked = subprocess.run(["git", "ls-files", "--", "*.sqlite3"], cwd=str(ROOT),
                             capture_output=True, text=True, check=True).stdout.split()
    assert not tracked, tracked
    ignored = subprocess.run(["git", "check-ignore", "-q", "db.sqlite3"], cwd=str(ROOT))
    assert ignored.returncode == 0, "db.sqlite3 is not ignored"


# ================================================================================= 9. CI
def _workflow():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "security.yml").read_text())


def test_ci_runs_the_suite_on_the_working_branch():
    """CI ran only on main and develop; the branch that holds the work is 446 commits
    ahead of main, so the suite, the migration check and the scans never ran on it."""
    flow = _workflow()
    triggers = flow.get("on") or flow.get(True)       # YAML 1.1 reads `on` as true
    for event in ("push", "pull_request"):
        assert "spec/payments-compliance" in triggers[event]["branches"], triggers[event]

    job = flow["jobs"]["test"]
    assert "postgres" in job["services"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert "pytest" in runs and "--create-db" in runs


def test_ci_every_script_a_step_runs_exists():
    """The import-sweep step ran `scripts/ci_import_sweep.py`, which does not exist."""
    flow = _workflow()
    missing = []
    for job in flow["jobs"].values():
        for step in job.get("steps", []):
            for path in re.findall(r"python\s+((?:[\w.-]+/)+[\w.-]+\.py)", step.get("run", "")):
                if "$" not in path and not (ROOT / path).exists():
                    missing.append(path)
    assert not missing, missing


# ============================================================================= 10. Sentry
_BOOT_WITH_LOGS = ("import logging.config\n"
                   "logging.config.dictConfig(s.LOGGING)\n"
                   "s.enforce_production_safety()\nprint('BOOTED')\n")


def test_sentry_absent_is_said_loudly_at_boot():
    proc = _run_prod(_BOOT_WITH_LOGS, SENTRY_DSN=None)
    assert proc.returncode == 0 and "BOOTED" in proc.stdout, proc.stderr[-2000:]
    warnings = [r for r in _json_lines(proc.stdout)
                if r["level"] == "WARNING" and "SENTRY_DSN" in r["msg"]]
    assert warnings, proc.stdout[-2000:]


def test_sentry_dsn_that_cannot_start_refuses_to_boot():
    """A DSN that Sentry rejects left the operator believing errors were tracked."""
    proc = _run_prod(_BOOT_WITH_LOGS, SENTRY_DSN="not a dsn")
    assert proc.returncode != 0 and "BOOTED" not in proc.stdout
    assert "SENTRY_DSN" in proc.stderr


_SENTRY_PROBE = r'''
import json
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.utils import match_regex_list
integration = sentry_sdk.get_client().get_integration(CeleryIntegration)
monitored = sorted(n for n in s.CELERY_BEAT_SCHEDULE
                   if not match_regex_list(n, integration.exclude_beat_tasks))
print(json.dumps({"monitor": integration.monitor_beat_tasks, "monitored": monitored}))
'''


def test_sentry_cron_monitors_exactly_the_money_jobs():
    """If the process that runs them dies, paid checkouts stay pending and coaching
    months neither renew nor freeze — and nothing said so."""
    proc = _run_prod(_SENTRY_PROBE, SENTRY_DSN="https://publickey@127.0.0.1:9/1")
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(next(line for line in proc.stdout.splitlines()
                             if line.startswith("{")))
    assert result["monitor"] is True
    assert set(result["monitored"]) == MONEY_ENTRIES, result
