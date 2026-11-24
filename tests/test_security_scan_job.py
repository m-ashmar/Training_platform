"""The `security-scan` job of .github/workflows/security.yml (fix run, 2026-11-21).

Two of its layers could not do their job.

**"Verify No Hardcoded Secrets" could never fail.** It ran `grep -rn "$PATTERNS"` with
`PATTERNS="sk-proj-|hf_V|django-insecure|..."`. Plain grep reads `|` as a literal, so it
searched for that whole string, found it nowhere and passed whatever was committed. It
now looks for the SHAPE of a key in every tracked file. These tests run the step's own
script, read out of the workflow, the way the runner does (`bash -e`), on a scratch
repository holding a key made here at run time. No key-shaped literal is ever written
into this repository: the scan reads this file too.

**Semgrep failed the job on nine checked false positives** (run 44749613904): five socket
addresses written with the plain ws scheme in documentation (production is TLS through
Caddy), and four places that are what they are on purpose, the Dockerfile's ENTRYPOINT
and CMD (no USER line: the entrypoint drops to appuser itself) and two raw `execute`
calls whose SQL text holds only a name from the model's `_meta`. Each of the four now
carries its reason and a `nosemgrep` line naming the rule. Semgrep itself is not run
here (`--config=auto` uploads the code to semgrep.dev): the CI run after the push is that
proof. These tests keep the text from coming back and each annotation on the line
Semgrep reads.
"""
import json
import os
import re
import secrets
import string
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "security.yml"
STEP = "Verify No Hardcoded Secrets"
ALNUM = string.ascii_letters + string.digits


# ======================================================================= the secret scan
def _step_script():
    flow = yaml.safe_load(WORKFLOW.read_text())
    steps = [s for s in flow["jobs"]["security-scan"]["steps"] if s.get("name") == STEP]
    assert len(steps) == 1, f"the security-scan job has no single step named {STEP!r}"
    return steps[0]["run"]


def _env(home, ceiling=None):
    """The runner's view of git: no user or system configuration (a `color.ui = always`
    would put escape codes into the output the script filters) and no GIT_* variable
    inherited from whatever started pytest."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    if ceiling is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    return env


def _scan(cwd, home, ceiling=None):
    script = home / "scan.sh"
    script.write_text(_step_script())
    # A `run:` block with no `shell:` runs as `bash -e {0}` on GitHub's Linux runners.
    return subprocess.run(["bash", "--noprofile", "--norc", "-e", str(script)],
                          cwd=str(cwd), env=_env(home, ceiling), capture_output=True,
                          text=True, timeout=120)


class _Scratch:
    """A new git repository. `track` writes files and adds them to the index (what
    `git ls-files` lists); `write` leaves them untracked; `scan` runs the step there."""

    def __init__(self, base):
        self.base, self.repo, self.home = base, base / "repo", base / "home"
        self.repo.mkdir()
        self.home.mkdir()
        self._git("init", "-q", ".")

    def _git(self, *args):
        subprocess.run(["git", *args], cwd=str(self.repo), env=_env(self.home, self.base),
                       check=True, capture_output=True)

    def write(self, files):
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def track(self, files):
        self.write(files)
        self._git("add", "--", *files)

    def scan(self):
        return _scan(self.repo, self.home, ceiling=self.base)


@pytest.fixture
def scratch(tmp_path):
    return _Scratch(tmp_path)


def _rand(alphabet, n):
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _one_line_key(shape):
    """A key of each one-line shape the step looks for, put together from its prefix and
    random characters now, so that no key-shaped literal exists in this file."""
    from django.core.management.utils import get_random_secret_key

    return {
        "openai-project": lambda: "sk-proj-" + _rand(ALNUM + "_-", 120),
        "openai-older": lambda: "sk-" + _rand(ALNUM, 48),
        "huggingface": lambda: "hf_" + _rand(ALNUM, 34),
        # Exactly what `django-admin startproject` writes into a new settings file.
        "django-generated": lambda: "django-insecure-" + get_random_secret_key(),
        "aws-access-key-id": lambda: "AKIA" + _rand(string.ascii_uppercase + string.digits, 16),
        "github-classic": lambda: "ghp_" + _rand(ALNUM, 36),
        "github-fine-grained": lambda: "github_pat_" + _rand(ALNUM, 22) + "_" + _rand(ALNUM, 59),
    }[shape]()


def _private_key_pem(kind):
    """A real private key, made now and thrown away with the scratch repository."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    if kind == "rsa-pkcs8":
        key, form = rsa.generate_private_key(65537, 2048), serialization.PrivateFormat.PKCS8
    elif kind == "rsa-pkcs1":
        key = rsa.generate_private_key(65537, 2048)
        form = serialization.PrivateFormat.TraditionalOpenSSL
    elif kind == "ec":
        key = ec.generate_private_key(ec.SECP256R1())
        form = serialization.PrivateFormat.TraditionalOpenSSL
    else:
        key, form = ed25519.Ed25519PrivateKey.generate(), serialization.PrivateFormat.OpenSSH
    return key.private_bytes(serialization.Encoding.PEM, form,
                             serialization.NoEncryption()).decode()


def _assert_found(proc, where, secret):
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)
    assert where in proc.stdout.splitlines(), f"the scan did not name {where}:\n{proc.stdout}"
    assert "::error::" in proc.stdout
    assert secret not in proc.stdout + proc.stderr, "the scan printed the key into the log"


def _assert_clean(proc):
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "No hardcoded secrets found." in proc.stdout


@pytest.mark.parametrize("shape", ["openai-project", "openai-older", "huggingface",
                                   "django-generated", "aws-access-key-id",
                                   "github-classic", "github-fine-grained"])
def test_a_committed_key_fails_the_scan(scratch, shape):
    """The old step passed every one of these (and anything else): its pattern was one
    literal string that no file contained."""
    key = _one_line_key(shape)
    scratch.track({"app/settings.py": f'import os\nAPI_KEY = os.environ.get("K", "{key}")\n'})
    _assert_found(scratch.scan(), "app/settings.py:2", key)


def test_a_key_in_a_file_that_is_not_python_fails_the_scan(scratch):
    """The old step read `*.py` only; a key pasted into a document, a YAML file or a
    shell script is just as public."""
    key = _one_line_key("openai-project")
    scratch.track({"docs/setup.md": f"# Setup\n\nexport AI_API_KEY={key}\n",
                   "app/views.py": "def index():\n    return 1\n"})
    _assert_found(scratch.scan(), "docs/setup.md:3", key)


@pytest.mark.parametrize("kind", ["rsa-pkcs8", "rsa-pkcs1", "ec", "openssh"])
def test_a_private_key_file_fails_the_scan(scratch, kind):
    pem = _private_key_pem(kind)
    scratch.track({"keys/server.pem": pem})
    _assert_found(scratch.scan(), "keys/server.pem:2", pem.splitlines()[1])


def test_a_private_key_indented_in_source_fails_the_scan(scratch):
    pem = _private_key_pem("rsa-pkcs1")
    scratch.track({"app/keys.py": 'KEY = """\n' + textwrap.indent(pem, "    ") + '"""\n'})
    _assert_found(scratch.scan(), "app/keys.py:3", pem.splitlines()[1])


def test_a_private_key_on_one_line_fails_the_scan(scratch):
    """A service-account JSON, or a .env value: the key's newlines written as `\\n`."""
    pem = _private_key_pem("rsa-pkcs8")
    scratch.track({"config/service-account.json": json.dumps(
        {"type": "service_account", "private_key": pem}) + "\n"})
    _assert_found(scratch.scan(), "config/service-account.json:1", pem.splitlines()[1])


def test_what_the_repository_writes_on_purpose_passes(scratch):
    """The placeholders and checks this repository holds deliberately, and things that
    look like keys and are not secret. A bare-word pattern (`django-insecure`, a PEM
    header alone) would fail every one of these, which is why `-E` alone was not the
    fix."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    public = rsa.generate_private_key(65537, 2048).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    scratch.track({
        # settings_production.py refuses a key Django generated.
        "settings_production.py": "if 'django-insecure' in (SECRET_KEY or ''):\n"
                                  "    errors.append('weak key')\n",
        # deploy/env.example: the generated key's place, with no key in it.
        "env.example": 'JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n'
                       '-----END PRIVATE KEY-----"\nOPENAI_API_KEY=\nHUGGINGFACE_API_TOKEN=\n',
        # deploy/setup.sh checks what openssl produced.
        "setup.sh": 'case "$JWT_PRIVATE_KEY" in "-----BEGIN PRIVATE KEY-----"*) ;; esac\n',
        # The tests' production boot environment.
        "boot.py": 'ENV = {"JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\nnot-a-key\\n'
                   '-----END PRIVATE KEY-----"}\n',
        # A public key is not a secret; a lock file's hashes are not keys.
        "jwt.pub": public,
        "requirements.lock": f"django==5.2.19 \\\n    --hash=sha256:{secrets.token_hex(32)}\n",
        "ai.py": 'OPENAI_API_KEY = get_env("OPENAI_API_KEY", "")\nprefix = "sk-"\n',
    })
    _assert_clean(scratch.scan())


def test_only_what_is_committed_is_read(scratch):
    """An untracked file (the server's or a laptop's .env) is not the repository's, and
    `_excluded/` is not part of the codebase (CLAUDE.md)."""
    scratch.track({"app/settings.py": "DEBUG = False\n",
                   "_excluded/old_script.py": f'KEY = "{_one_line_key("openai-older")}"\n'})
    scratch.write({".env": f"AI_API_KEY={_one_line_key('openai-project')}\n"})
    _assert_clean(scratch.scan())


def test_a_scan_that_cannot_run_fails_the_job(tmp_path):
    """Outside a git checkout `git grep` cannot read anything; that must not read as a
    clean tree, the very failure the old step had."""
    plain, home = tmp_path / "plain", tmp_path / "home"
    plain.mkdir()
    home.mkdir()
    (plain / "settings.py").write_text(f'KEY = "{_one_line_key("openai-older")}"\n')
    proc = _scan(plain, home, ceiling=tmp_path)
    assert proc.returncode not in (0, 1), (proc.returncode, proc.stdout, proc.stderr)
    assert "did not run" in proc.stdout


def _in_a_checkout():
    probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(ROOT),
                           capture_output=True, text=True)
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def test_this_repository_passes_its_own_scan(tmp_path):
    if not _in_a_checkout():
        pytest.skip("not a git checkout: there are no tracked files to scan")
    _assert_clean(_scan(ROOT, tmp_path))


# ============================================================================== Semgrep
def _tracked_text(skip=("_excluded/",)):
    """Every tracked text file as (path, text): what Semgrep's checkout holds."""
    if not _in_a_checkout():
        pytest.skip("not a git checkout: there are no tracked files to read")
    names = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT), capture_output=True,
                           check=True).stdout.decode().split("\0")
    for name in names:
        path = ROOT / name
        if not name or name.startswith(skip) or not path.is_file():
            continue            # the flutter gitlink is a directory, not a file
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue            # binary: the .mo catalogue, images
        yield name, data.decode("utf-8", errors="replace")


# Semgrep's detect-insecure-websocket: the plain scheme at a word start, anywhere in any
# text file; a local address is not its concern. Spelt `/{2}` so this file holds none.
_PLAIN_WS = re.compile(r"(?<![A-Za-z0-9_])ws:/{2}(?!localhost|127\.0\.0\.1)")


def test_no_tracked_text_writes_a_socket_address_with_the_plain_ws_scheme():
    found = [f"{name}:{n}" for name, text in _tracked_text()
             for n, line in enumerate(text.splitlines(), 1) if _PLAIN_WS.search(line)]
    assert not found, ("production serves the sockets over TLS (wss://<host>/ws/...); "
                       "these lines fail Semgrep's ERROR rule: " + ", ".join(found))


_RAW_QUERY = "python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query"
_ANNOTATED = [
    # (file, how the finding's first line starts, the rule it is a false positive of)
    ("Dockerfile", "ENTRYPOINT ",
     "dockerfile.security.missing-user-entrypoint.missing-user-entrypoint"),
    ("Dockerfile", "CMD ", "dockerfile.security.missing-user.missing-user"),
    ("users/migrations/0026_encrypt_existing_injury_data.py", "cursor.execute(", _RAW_QUERY),
    ("wallet/models.py", 'cur.execute(f"SELECT 1 FROM {table} WHERE {pk}', _RAW_QUERY),
]


@pytest.mark.parametrize("path,start,rule", _ANNOTATED)
def test_each_checked_false_positive_is_annotated_on_the_line_semgrep_reads(path, start, rule):
    """Semgrep honours `nosemgrep` on the finding's own line or on the line just before
    it, and in a Dockerfile only a line of its own is a comment. A comment slipped in
    between, or an instruction moved, brings the finding back."""
    lines = (ROOT / path).read_text().splitlines()
    at = [i for i, line in enumerate(lines) if line.lstrip().startswith(start)]
    assert len(at) == 1, f"{path}: expected one line starting {start!r}, found {len(at)}"
    i = at[0]
    assert lines[i - 1].strip() == f"# nosemgrep: {rule}", (
        f"{path}:{i + 1} is not annotated on the line before it: {lines[i - 1]!r}")
    reason = lines[i - 2].strip()
    assert reason.startswith("#") and len(reason) > 20, (
        f"{path}:{i + 1}: the annotation gives no reason above it")


def test_every_nosemgrep_names_one_rule_and_gives_its_reason():
    """A bare `nosemgrep` silences every rule on its line, today's and tomorrow's.
    (Documentation may show the form in prose; it annotates nothing.)"""
    annotation = re.compile(r"#\s*nosem(?:grep)?\b(.*)$")
    bad = []
    for name, text in _tracked_text(skip=("_excluded/", "tests/")):
        if name.endswith(".md"):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            m = annotation.search(line)
            if not m:
                continue
            if not re.fullmatch(r":\s*[a-z0-9_-]+(?:\.[a-z0-9_-]+)+\s*", m.group(1)):
                bad.append(f"{name}:{i + 1} names no single full rule id")
            elif line.lstrip().startswith("#") and not (
                    i and lines[i - 1].lstrip().startswith("#")):
                bad.append(f"{name}:{i + 1} has no reason on the line above")
    assert not bad, bad
