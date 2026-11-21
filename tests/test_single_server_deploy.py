"""The single-server deployment (`deploy/`): one machine, Docker Compose, Caddy in front.

Nothing here starts a container. These are the properties of the files that, when
wrong, are found on the server at the worst moment or not found at all:

1. the secrets file can never reach the image or the repository;
2. the compose file runs the SAME processes `fly.toml` does, publishes only the proxy,
   and migrates before anything serves;
3. certificates come from Let's Encrypt and from nowhere else;
4. the environment `deploy/setup.sh` writes is one production actually boots on.
"""
import os
import re
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.test_media_storage import _production_boot

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"

APP_SERVICES = ("migrate", "web", "worker", "beat")


def _compose():
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())


def _lines(path):
    return {line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")}


# ===========================================================================
# 1. The secrets file stays on the server
# ===========================================================================

def test_the_secrets_file_cannot_reach_the_image_or_the_repository():
    """`.dockerignore` patterns are anchored at the root: `.env` there never matched
    `deploy/.env`, and `COPY . .` would have baked every key into the image."""
    ignored = _lines(ROOT / ".dockerignore")
    assert "deploy/" in ignored
    assert "**/.env" in ignored

    for name in ("deploy/.env", "deploy/.env.Ab12Cd"):       # the file, and setup.sh's temp
        check = subprocess.run(["git", "check-ignore", "-q", name], cwd=str(ROOT))
        assert check.returncode == 0, f"{name} is not ignored by git"

    tracked = subprocess.run(["git", "ls-files", "--", "deploy"], cwd=str(ROOT),
                             capture_output=True, text=True, check=True).stdout.split()
    assert "deploy/.env" not in tracked


def test_the_template_carries_no_secret():
    """`env.example` shows the shape. Every secret in it is empty or a placeholder."""
    text = (DEPLOY / "env.example").read_text()
    for name in ("DB_PASSWORD", "REDIS_PASSWORD", "DJANGO_SECRET_KEY", "FIELD_ENCRYPTION_KEY",
                 "AI_TRAINING_PSEUDONYM_KEY", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
                 "EMAIL_HOST_PASSWORD"):
        assert re.search(rf"^{name}=''$", text, re.M), f"{name} is not empty in the template"
    assert not re.search(r"^[A-Za-z0-9+/]{40,}$", text, re.M), "key material in the template"


# ===========================================================================
# 2. The compose file
# ===========================================================================

def test_the_compose_file_runs_the_processes_fly_runs():
    """Two descriptions of one deployment drift apart silently. The commands are
    compared word for word; only the beat schedule's path may differ."""
    fly = tomllib.loads((ROOT / "fly.toml").read_text())
    services = _compose()["services"]
    for name in ("web", "worker", "beat"):
        assert services[name]["command"] == fly["processes"][name].split(), name
    assert " ".join(services["migrate"]["command"]) == fly["deploy"]["release_command"]

    env = services["web"]["environment"]
    assert env["DJANGO_SETTINGS_MODULE"] == fly["env"]["DJANGO_SETTINGS_MODULE"]
    assert env["FILE_UPLOAD_TEMP_DIR"] == fly["env"]["FILE_UPLOAD_TEMP_DIR"]
    # Every app service is the same image with the same environment.
    for name in APP_SERVICES:
        assert services[name]["image"] == services["web"]["image"], name
        assert services[name]["environment"] == env, name
        assert services[name]["env_file"] == ".env", name


def test_exactly_one_scheduler_and_nothing_serves_before_the_migration():
    services = _compose()["services"]
    beats = [name for name, service in services.items()
             if "beat" in (service.get("command") or [])]
    assert beats == ["beat"], "a second beat sends every scheduled job twice"
    assert "deploy" not in services["beat"], "beat must not be replicated"

    for name in ("web", "worker", "beat"):
        needs = services[name]["depends_on"]
        assert needs["migrate"]["condition"] == "service_completed_successfully", name
        assert needs["db"]["condition"] == "service_healthy", name
        assert needs["redis"]["condition"] == "service_healthy", name
    assert services["migrate"]["restart"] == "no"
    assert "migrate" not in services["migrate"]["depends_on"]


def test_only_the_proxy_is_reachable_from_outside():
    """Postgres and Redis on a public port are found by scanners within the hour. A
    published port also bypasses the host firewall: Docker writes its own rules."""
    services = _compose()["services"]
    published = {name: service["ports"] for name, service in services.items()
                 if service.get("ports")}
    assert set(published) == {"caddy"}, published
    assert sorted(published["caddy"]) == ["443:443", "80:80"]
    assert all("network_mode" not in service for service in services.values())


def test_redis_keeps_queued_jobs_and_media_is_object_storage():
    services = _compose()["services"]
    redis = services["redis"]["command"]
    assert redis[redis.index("--maxmemory-policy") + 1] == "noeviction", \
        "an evicting Redis deletes queued jobs (training_platform/redis_layout.py)"
    assert "--requirepass" in redis

    env = services["web"]["environment"]
    assert env["MEDIA_BACKEND"] == "s3"
    assert "@redis:6379" in env["REDIS_URL"] and not env["REDIS_URL"].rstrip("/")[-2:] == "/0"
    # No upload lives on this machine: the volumes are the database, the queue and the
    # certificates, and none is mounted into the app.
    assert all("volumes" not in services[name] for name in APP_SERVICES)
    # The proxy's configuration is mounted as a folder and read-only, and its
    # certificates are kept: Let's Encrypt limits how often it will issue a new one.
    assert "./caddy:/etc/caddy:ro" in services["caddy"]["volumes"]
    assert "caddy_data:/data" in services["caddy"]["volumes"]


def test_no_secret_is_written_in_the_compose_file():
    """Every secret is a ${VARIABLE} read from deploy/.env, and a missing one stops
    `docker compose up` with a message instead of starting Postgres with no password."""
    text = (DEPLOY / "docker-compose.yml").read_text()
    for name in ("DB_PASSWORD", "REDIS_PASSWORD"):
        assert f"${{{name}:?" in text, f"{name} may be empty without an error"
    literal = re.search(r"^\s*\w*(PASSWORD|SECRET|KEY)\w*:\s*(?!\$\{)\S.*$", text, re.M)
    assert not literal, f"a literal secret: {literal.group(0).strip()}"


# ===========================================================================
# 3. Certificates
# ===========================================================================

def test_certificates_come_from_lets_encrypt_and_nowhere_else():
    """Caddy's default is Let's Encrypt with a fallback to ZeroSSL, and ZeroSSL refuses
    Syria. A renewal that falls through to it is an outage nobody was warned about."""
    code = "\n".join(line for line in (DEPLOY / "caddy" / "Caddyfile").read_text().splitlines()
                     if not line.strip().startswith("#"))
    issuers = re.findall(r"^\s*cert_issuer\s+(\S+)", code, re.M)
    assert issuers == ["acme"], f"the issuer list is {issuers}, not Let's Encrypt alone"
    assert "https://acme-v02.api.letsencrypt.org/directory" in code
    assert "zerossl" not in code.lower()
    # Caddy adds its ZeroSSL default only when a GLOBAL email is set: the address is
    # inside the issuer, and nowhere else.
    assert not re.search(r"^\s*email\s", code.split("cert_issuer")[0], re.M)
    assert re.search(r"cert_issuer acme \{[^}]*email \{\$ACME_EMAIL\}", code, re.S)
    assert not re.search(r"^\s*(acme_ca|tls)\b", code, re.M)
    assert "{$API_DOMAIN}" in code and "{$ACME_EMAIL}" in code
    assert re.search(r"reverse_proxy\s+web:8000", code)
    # No HTTP/3: only TCP is published.
    assert re.search(r"protocols\s+h1\s+h2\s*$", code, re.M)


# ===========================================================================
# 4. setup.sh writes an environment production boots on
# ===========================================================================

ANSWERS = [
    "api.staging-yg.test",                     # the API's host name
    "ops@staging-yg.test",                     # certificate notices
    "",                                        # the S3 address: the default
    "",                                        # the region: taken from the address
    "yg-media-test",                           # the bucket
    "ACCESSKEYFORTHETEST",                     # its access key
    "secret-key-for-the-test",                 # its secret key
    "", "",                                    # SMTP host and user: the defaults
    "smtp-password-for-the-test",              # SMTP password
    "",                                        # the From address: the default
    "",                                        # no AI key
    "",                                        # no Sentry DSN
    "",                                        # no signing fingerprint yet
    "",                                        # no Apple team id yet
]


def _read_env_file(path):
    """The format setup.sh writes: KEY='value', and KEY="several lines" for the PEMs."""
    values, text = {}, path.read_text()
    for match in re.finditer(r"^([A-Z0-9_]+)='([^']*)'$", text, re.M):
        values[match.group(1)] = match.group(2)
    for match in re.finditer(r'^([A-Z0-9_]+)="([^"]*)"$', text, re.M | re.S):
        values[match.group(1)] = match.group(2)
    return values


@pytest.fixture(scope="module")
def written(tmp_path_factory):
    work = tmp_path_factory.mktemp("deploy")
    script = work / "setup.sh"
    script.write_bytes((DEPLOY / "setup.sh").read_bytes())
    script.chmod(0o755)
    ran = subprocess.run(["bash", str(script)], input="\n".join(ANSWERS) + "\n",
                         cwd=str(work), capture_output=True, text=True, timeout=120)
    assert ran.returncode == 0, ran.stderr + ran.stdout
    return work, ran


def test_setup_writes_a_private_file_and_never_shows_a_secret(written):
    work, ran = written
    target = work / ".env"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    values = _read_env_file(target)
    for secret in ("DB_PASSWORD", "REDIS_PASSWORD", "DJANGO_SECRET_KEY",
                   "FIELD_ENCRYPTION_KEY", "S3_SECRET_ACCESS_KEY", "EMAIL_HOST_PASSWORD"):
        assert values[secret], secret
        assert values[secret] not in ran.stdout + ran.stderr, f"{secret} was printed"
    assert values["JWT_PRIVATE_KEY"].startswith("-----BEGIN PRIVATE KEY-----")
    assert values["JWT_PUBLIC_KEY"].rstrip().endswith("-----END PUBLIC KEY-----")
    # Generated here, so two servers never share a key.
    assert len({values["DB_PASSWORD"], values["REDIS_PASSWORD"],
                values["DJANGO_SECRET_KEY"], values["AI_TRAINING_PSEUDONYM_KEY"]}) == 4
    # An optional variable that was skipped is ABSENT, not empty: an empty one is the
    # empty string to the settings, and replaces the default.
    assert "AI_API_KEY" not in values and "SENTRY_DSN" not in values
    assert not re.search(r"^[A-Z0-9_]+=('')?$", target.read_text(), re.M)
    assert values["S3_REGION_NAME"] == "fsn1"
    assert values["DEFAULT_FROM_EMAIL"] == "Yalla Gym <no-reply@staging-yg.test>"


def test_setup_never_overwrites_the_keys(written):
    """The file holds FIELD_ENCRYPTION_KEY. A second run that replaced it would make the
    health data already in the database unreadable for ever."""
    work, _ = written
    before = (work / ".env").read_bytes()
    again = subprocess.run(["bash", str(work / "setup.sh")], input="\n".join(ANSWERS) + "\n",
                           cwd=str(work), capture_output=True, text=True, timeout=120)
    assert again.returncode != 0
    assert "never overwrites" in again.stderr
    assert (work / ".env").read_bytes() == before
    assert [p.name for p in work.iterdir() if p.name.startswith(".env.")] == []


@pytest.mark.django_db(transaction=False)
def test_production_boots_on_what_setup_wrote(written):
    """The whole point: the file plus the compose file's own environment is a
    configuration `enforce_production_safety` accepts, with nothing added by hand."""
    work, _ = written
    values = _read_env_file(work / ".env")
    compose_env = _compose()["services"]["web"]["environment"]
    env = dict(values)
    for key, value in compose_env.items():
        value = str(value)
        value = re.sub(r"\$\{(\w+):[-?][^}]*\}", lambda m: values.get(m.group(1), ""), value)
        defaults = re.fullmatch(r"\$\{(\w+):-([^}]*)\}", str(compose_env[key]))
        if defaults and not values.get(defaults.group(1)):
            value = defaults.group(2)
        env[key] = value
    assert env["REDIS_URL"] == f"redis://:{values['REDIS_PASSWORD']}@redis:6379"

    booted = _production_boot(**env)
    assert booted.returncode == 0, booted.stderr
    assert "BOOTED" in booted.stdout

    # And it is the S3 store it booted on: take the bucket away and it refuses.
    env.pop("S3_BUCKET_NAME")
    refused = _production_boot(**env)
    assert refused.returncode != 0 and "S3_BUCKET_NAME" in refused.stderr


def _server_env(values):
    """What a server running ONLY setup.sh's file and this compose file hands the web
    process: the file's values, then the compose file's own `environment`, interpolated
    the way Compose does the two simple forms it uses (`${X:-default}`, `${X:?message}`),
    and nothing else."""
    env = dict(values)
    for key, raw in _compose()["services"]["web"]["environment"].items():
        value = re.sub(r"\$\{(\w+):[-?][^}]*\}", lambda m: values.get(m.group(1), ""),
                       str(raw))
        defaults = re.fullmatch(r"\$\{(\w+):-([^}]*)\}", str(raw))
        if defaults and not values.get(defaults.group(1)):
            value = defaults.group(2)
        env[key] = value
    env["PATH"] = os.environ.get("PATH", "")
    return env


@pytest.mark.django_db(transaction=False)
def test_production_boots_on_setup_and_compose_alone(written):
    """The test above boots through the shared test environment (`_production_boot`),
    which supplies every required variable itself: a variable setup.sh forgets is filled
    in there, and that test still passes. This one boots on the file and the compose
    file's environment ALONE, which is the server's situation. It caught
    PUBLIC_PAGES_BASE_URL (required in production since BACKEND_BUGS B-174) missing from
    setup.sh: merged into the deployed line on 2026-11-21, the live server would have
    refused to boot at its next update."""
    work, _ = written
    env = _server_env(_read_env_file(work / ".env"))
    code = ("import training_platform.settings_production as s; "
            "s.enforce_production_safety(); print('BOOTED')")
    booted = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                            capture_output=True, text=True, timeout=120)
    assert booted.returncode == 0, booted.stderr
    assert "BOOTED" in booted.stdout


def test_setup_writes_the_public_pages_base_and_the_apps_identifiers(written):
    """The legal links are built on the API host until a hub domain exists, and the
    association files name this app. The signing fingerprint and the Apple team id are
    not known at setup time, so they are not written: the two files answer 404
    `app_links_not_configured` until they are added (releases/app_links.py)."""
    work, _ = written
    values = _read_env_file(work / ".env")
    assert values["PUBLIC_PAGES_BASE_URL"] == "https://api.staging-yg.test"
    assert values["APP_LINKS_ANDROID_PACKAGES"] == "com.yallagym.app"
    assert values["APP_LINKS_IOS_BUNDLE_IDS"] == "com.yallagym.app"
    assert values["APP_LINKS_PATHS"] == "/app/*"
    assert "APP_LINKS_ANDROID_SHA256_FINGERPRINTS" not in values
    assert "APP_LINKS_IOS_TEAM_ID" not in values


def test_setup_refuses_an_address_that_is_not_a_host_name(tmp_path):
    script = tmp_path / "setup.sh"
    script.write_bytes((DEPLOY / "setup.sh").read_bytes())
    for bad in ("https://api.yallagym.test", "localhost", "api.yallagym.test/path"):
        ran = subprocess.run(["bash", str(script)], input=bad + "\n", cwd=str(tmp_path),
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode != 0, bad
        assert not (tmp_path / ".env").exists(), bad
        assert not [p for p in os.listdir(tmp_path) if p.startswith(".env")], bad
