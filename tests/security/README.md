# Security probes (Phase 7)

Executable proofs for the authorization findings fixed in Phase 7. Each script
spins up an isolated test database, seeds an attacker/victim/trainer/admin, and
asserts both directions:

* `phase7_proof.py`    — read-side IDORs must be blocked (analytics, sessions, routine-exercises, profile email)
* `phase7_write.py`    — write-side: cross-trainer session modify/create must be blocked
* `phase7_positive.py` — legitimate access MUST still work (client/trainer/admin) — 14 checks
* `routine_dive.py`    — exercise visibility + cross-trainer template hijack must be blocked
* `routine_pos.py`     — legitimate exercise visibility + template read/copy/edit/delete — 7 checks
* `global_ex2.py`      — global-exercise media is admin-only; owners keep access

Run (local Postgres on :5433 must be up):
    .venv/bin/python tests/security/phase7_positive.py

These are standalone scripts, not yet pytest cases — Phase 14 converts them.
