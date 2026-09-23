"""Run the audit probes in this directory and give each one a verdict.

    .venv/bin/python tests/security/run_probes.py                     # every probe
    .venv/bin/python tests/security/run_probes.py sweep_5xx p9_privacy
    .venv/bin/python tests/security/run_probes.py --list              # what each can say

Nothing ran these. `pytest.ini` collects `test_*.py` and no probe is named so, CI ran
four of them and only on a push to `main`, and every one of them exits 0 whatever it
prints — `*** REGRESSION ***` included. This reads what each probe prints and turns it
into a verdict, so running all of them is one command with one exit status:

    failed  the probe crashed, timed out, or printed one of its own failure marks
    passed  it prints a verdict (PASS/FAIL lines, or a 5xx count) and none of it failed
    ran     it prints observations only: nothing in it can fail, and a person reads its
            log to know what it saw

Exit status 1 if any probe failed, else 0. A `ran` probe is not a pass; the summary
counts the two separately so it cannot be mistaken for one.

Each probe runs in its own process on its own database: `DB_NAME` is suffixed with
`_probe`, so the `test_<name>` database a probe builds and drops is never the one a
pytest run on the same `DB_NAME` is using. Run it alone — one Postgres serves every
worktree.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # THIS tree, like every script here (S-008 checks it)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

#: A line any probe prints when something it checks has gone wrong. Each pattern is one
#: a probe here actually uses; see the README.
FAILURE_MARKS = (
    re.compile(r"\[FAIL\]"),                         # [PASS]/[FAIL] checklists
    re.compile(r"\*\*\*\s*FAIL"),                    # *** FAIL ***, *** FAILS OPEN
    re.compile(r"\*\*\*\s*REGRESSION\s*\*\*\*"),     # phase7_positive, routine_pos
    re.compile(r"FIX FAILED"),                       # dive_verify_db
    re.compile(r"5xx\s*/\s*exceptions\s*[:=]\s*[1-9]"),   # the route sweeps
    re.compile(r"\bFAIL=[1-9]"),                     # RESULT: PASS=n FAIL=m
)

#: A line that shows the probe prints a verdict at all, as opposed to observations.
VERDICT_MARKS = (
    re.compile(r"\bPASS\b"),
    re.compile(r"5xx\s*/\s*exceptions\s*[:=]\s*\d"),
)


def discover() -> list[Path]:
    """Every probe in this directory: each `.py` except this runner."""
    return sorted(p for p in HERE.glob("*.py")
                  if p.name != Path(__file__).name and not p.name.startswith("test_"))


def carries_a_verdict(source: str) -> bool:
    """Does the probe's source print anything a verdict can be read from?"""
    return any(mark in source for mark in ("PASS", "FAIL", "REGRESSION", "5xx"))


def verdict(returncode: int | None, output: str) -> tuple[str, str]:
    """`(status, reason)` for one probe's exit status and everything it printed.

    `returncode` None means it was killed for running past the timeout.
    """
    if returncode is None:
        return "failed", "timed out"
    for line in output.splitlines():
        for mark in FAILURE_MARKS:
            if mark.search(line):
                return "failed", line.strip()[:160]
    if returncode != 0:
        tail = [line for line in output.splitlines() if line.strip()][-1:] or [""]
        return "failed", f"exit {returncode}: {tail[0].strip()[:140]}"
    if any(mark.search(output) for mark in VERDICT_MARKS):
        return "passed", ""
    return "ran", "observations only; read the log"


def probe_env(environ=None) -> dict:
    """The environment a probe runs in: the caller's, on a database of its own."""
    env = dict(os.environ if environ is None else environ)
    env["DB_NAME"] = f"{env.get('DB_NAME') or 'training_platform'}_probe"
    env.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings_local")
    return env


def run(probe: Path, *, timeout: int, logs: Path) -> tuple[str, str, float]:
    started = time.monotonic()
    try:
        done = subprocess.run([sys.executable, str(probe)], cwd=ROOT, env=probe_env(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout)
        code, output = done.returncode, done.stdout
    except subprocess.TimeoutExpired as exc:
        code = None
        output = exc.stdout if isinstance(exc.stdout, str) else (
            (exc.stdout or b"").decode("utf-8", "replace"))
    (logs / f"{probe.stem}.log").write_text(output or "", encoding="utf-8")
    status, reason = verdict(code, output or "")
    return status, reason, time.monotonic() - started


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("probes", nargs="*", help="probe names (default: all)")
    parser.add_argument("--timeout", type=int, default=600, help="seconds per probe")
    parser.add_argument("--logs", type=Path, default=None,
                        help="where each probe's output goes (default: a new temp dir)")
    parser.add_argument("--list", action="store_true",
                        help="list the probes and whether each prints a verdict")
    args = parser.parse_args(argv)

    found = {p.stem: p for p in discover()}
    unknown = [name for name in args.probes if name not in found]
    if unknown:
        parser.error(f"no such probe: {', '.join(unknown)}")
    chosen = [found[name] for name in args.probes] if args.probes else list(found.values())

    if args.list:
        for probe in chosen:
            kind = ("verdict" if carries_a_verdict(probe.read_text(encoding="utf-8"))
                    else "observations")
            print(f"{probe.stem:28} {kind}")
        return 0

    logs = args.logs or Path(tempfile.mkdtemp(prefix="security-probes-"))
    logs.mkdir(parents=True, exist_ok=True)
    counts = {"passed": 0, "ran": 0, "failed": 0}
    for probe in chosen:
        status, reason, seconds = run(probe, timeout=args.timeout, logs=logs)
        counts[status] += 1
        print(f"{status.upper():7} {probe.stem:28} {seconds:6.1f}s  {reason}", flush=True)
    print(f"\npassed={counts['passed']} ran={counts['ran']} failed={counts['failed']}"
          f"  logs: {logs}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
