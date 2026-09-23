# Audit probes

These scripts are the evidence audits left behind, not a test suite. Each one was
written to show one finding when it was found. It builds its own database, seeds the
people it needs, makes its requests and prints what it saw. None of them was written to
fail: every probe exits 0 whatever it prints, `*** REGRESSION ***` included, and most
print observations with nothing to pass or fail.

What the platform relies on is the regression gate, `tests/test_*.py`. When a probe's
finding needs protecting, it becomes an assertion there. That is where most of the gate
came from (`tests/conftest.py`). A probe is never converted in place.

## What runs them

| Where | What | When |
|---|---|---|
| `pytest` | none of them. `pytest.ini` collects `test_*.py`, and no probe has that name | — |
| CI, `full-suite` job | `sweep_5xx`, `sweep_anon`, `detail_sweep`, `dive2_write_sweep`. It fails on any 5xx | only on a push to `main` |
| `run_probes.py` | every probe, or the ones you name. Each gets its own process and database, and a verdict | when you run it |

```bash
docker compose up -d db
.venv/bin/python tests/security/run_probes.py --list                 # which probes print a verdict
.venv/bin/python tests/security/run_probes.py sweep_5xx p9_privacy   # some of them
.venv/bin/python tests/security/run_probes.py                        # all of them (slow)
```

Run it on its own, never beside a pytest run or another worktree's run: one Postgres
serves all of them. Each probe runs with `DB_NAME=<yours>_probe`, so the database it
builds and drops is never the one your pytest run is using.

## Verdicts

| Verdict | Meaning |
|---|---|
| `failed` | The probe crashed or timed out, or it printed one of its own failure marks: `[FAIL]`, `*** FAIL`, `*** REGRESSION ***`, `FIX FAILED`, `FAIL=<n>`, or a 5xx count above zero |
| `passed` | The probe prints a verdict, either PASS/FAIL lines or a 5xx count, and nothing in it failed |
| `ran` | The probe prints observations only. Nothing in it can fail, so read its log. This is not a pass, and the summary counts it separately |

`run_probes.py` exits 1 if any probe failed.

## The Phase 7 proofs

The first probes here, for the authorization findings fixed in Phase 7:

* `phase7_proof.py`: read-side IDORs (analytics, sessions, routine-exercises, profile email)
* `phase7_write.py`: write-side, a trainer modifying or creating another trainer's client session
* `phase7_positive.py`: legitimate access still works for client, trainer and admin (14 checks)
* `routine_dive.py`: exercise visibility, and cross-trainer template hijack
* `routine_pos.py`: legitimate exercise visibility, and template read, copy, edit and delete (7 checks)
* `global_ex2.py`: global-exercise media is admin-only, and owners keep access

## Adding a probe

Start it with the same `sys.path.insert(...)` line every script here uses. It puts
this tree on the path, so a worktree runs its own code. `tests/test_side_bugs_round1.py`
checks this. Print a failure mark from the table above when something is wrong, so
`run_probes.py` can see it. If the finding matters, write the test in `tests/test_*.py`
as well.
