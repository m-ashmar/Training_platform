"""End-to-end smoke of the main app journeys, over real HTTP, against a running server.

Not a test of the suite's kind: it drives a live server (a local Daphne on a fresh
database, a staging deploy) the way the app does, and reports what answered and how.
It WRITES a little data (a checkout session, a post, a set log), so point it only at a
local or staging server seeded with `bootstrap_platform` and `seed_review_accounts`,
never at production.

    SMOKE_BASE_URL=http://127.0.0.1:8031 \\
    REVIEW_ACCOUNTS_EMAIL='review-{role}@example.test' \\
    REVIEW_ACCOUNTS_PASSWORD='…' \\
    .venv/bin/python tools/smoke_journeys.py

The addresses and the password come from the environment, as `seed_review_accounts`
takes them — never from this file. Exit status 1 when any step answered outside what it
expects. The report lists every step: role, method, path, build, status, expected, and a
short note.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import requests

BASE = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8031").rstrip("/")
EMAIL = os.environ.get("REVIEW_ACCOUNTS_EMAIL", "")
PASSWORD = os.environ.get("REVIEW_ACCOUNTS_PASSWORD", "")
TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "30"))


def address(role: str) -> str:
    """The review account's address, derived exactly as `seed_review_accounts` does."""
    if "{role}" in EMAIL:
        return EMAIL.replace("{role}", role)
    local, _at, domain = EMAIL.partition("@")
    return f"{local}+{role}@{domain}"


RESULTS: list[dict] = []


def record(role, method, path, build, status, ok, note=""):
    RESULTS.append({"role": role, "method": method, "path": path, "build": build,
                    "status": status, "ok": ok, "note": note})


def call(session, role, method, path, *, build="apk", expect=(200,), body=None,
         note_from=None):
    headers = {"X-App-Channel": build, "X-App-Version": "1.0.0+1",
               "Accept-Language": "ar"}
    if session.get("token"):
        headers["Authorization"] = f"Bearer {session['token']}"
    try:
        resp = requests.request(method, BASE + path, headers=headers,
                                json=body if body is not None else None, timeout=TIMEOUT)
    except requests.RequestException as exc:
        record(role, method, path, build, "ERR", False, f"{type(exc).__name__}: {exc}")
        return None
    ok = resp.status_code in expect
    note = ""
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not ok:
        note = (json.dumps(data, ensure_ascii=False)[:240] if data is not None
                else resp.text[:240])
    elif note_from and data is not None:
        try:
            note = str(note_from(data))[:240]
        except Exception as exc:  # a note is a courtesy
            note = f"(note failed: {exc})"
    record(role, method, path, build, resp.status_code, ok, note)
    return data if ok else None


def login(role):
    session = {}
    data = call(session, role, "POST", "/api/auth/login/",
                body={"email": address(role), "password": PASSWORD},
                note_from=lambda d: f"user_type={d.get('user', {}).get('user_type')}")
    if data:
        session["token"] = data.get("access")
    return session


def journeys():
    anon = {}
    # Public surfaces.
    call(anon, "anon", "GET", "/api/auth/health/")
    call(anon, "anon", "GET", "/api/v1/config/capabilities", build="apk",
         note_from=lambda d: {k: v for k, v in d.items() if "price" in k or "can_" in k}
         if isinstance(d, dict) else d)
    call(anon, "anon", "GET", "/api/v1/config/capabilities", build="ios")
    call(anon, "anon", "GET", "/api/v1/app/latest?channel=apk&build=1")
    call(anon, "anon", "GET", "/legal/privacy/?lang=ar", expect=(200,))
    call(anon, "anon", "GET", "/legal/terms/?lang=en", expect=(200,))
    call(anon, "anon", "GET", "/.well-known/assetlinks.json", expect=(200, 404))

    # The client.
    client = login("client")
    if client.get("token"):
        call(client, "client", "GET", "/api/v1/home",
             note_from=lambda d: [s.get("type") for s in d.get("sections", d)
                                  if isinstance(s, dict)][:12]
             if isinstance(d, (dict, list)) else d)
        call(client, "client", "GET", "/api/subscription/v1/subscriptions/current/")
        plans = call(client, "client", "GET", "/api/subscription/v1/plans/",
                     note_from=lambda d: len(d.get("results", d)) if d else 0)
        call(client, "client", "GET", "/api/wallet/balance/")
        call(client, "client", "GET", "/api/wallet/transactions/")
        call(client, "client", "GET", "/api/routine/routines/")
        call(client, "client", "GET", "/api/routine/workout-sessions/")
        call(client, "client", "GET", "/api/routine/analytics/summary/")
        call(client, "client", "GET", "/api/diet/v1/preferences/")
        call(client, "client", "GET", "/api/diet/v1/my/diet-plans/")
        call(client, "client", "GET", "/api/v1/chat/conversations/")
        call(client, "client", "GET", "/api/social/challenges/")
        call(client, "client", "GET", "/api/social/posts/")
        call(client, "client", "POST", "/api/social/posts/", expect=(200, 201),
             body={"content": f"smoke {uuid.uuid4().hex[:6]} — تمرين اليوم"})
        # A plan checkout: sold on apk, refused on ios (rule zero, by build).
        plan_list = (plans or {}).get("results", plans) if plans else []
        plan_id = plan_list[0]["id"] if plan_list else None
        if plan_id:
            call(client, "client", "POST", "/api/v1/checkout/sessions/", build="apk",
                 expect=(200, 201, 409),
                 body={"money_type": "PLATFORM_SUB", "item_id": plan_id},
                 note_from=lambda d: {k: d.get(k) for k in ("status", "reference")})
            call(client, "client", "POST", "/api/v1/checkout/sessions/", build="ios",
                 expect=(403,),
                 body={"money_type": "PLATFORM_SUB", "item_id": plan_id})

    # The trainer.
    trainer = login("trainer")
    if trainer.get("token"):
        call(trainer, "trainer", "GET", "/api/auth/trainer/pending-requests/")
        call(trainer, "trainer", "GET", "/api/v1/trainer/attention/")
        call(trainer, "trainer", "GET", "/api/v1/trainer/verification/")
        call(trainer, "trainer", "GET", "/api/v1/trainer/withdrawals/setup/")
        call(trainer, "trainer", "GET", "/api/v1/trainer/products/")
        call(trainer, "trainer", "GET", "/api/wallet/balance/",
             note_from=lambda d: {k: d.get(k) for k in ("balance", "available",
                                                          "settling", "held_for_trainer")})

    # The agent.
    agent = login("agent")
    if agent.get("token"):
        call(agent, "agent", "GET", "/api/wallet/agent/me/")
        call(agent, "agent", "GET", "/api/wallet/agent/ledger/")


def main():
    if not EMAIL or not PASSWORD:
        print("Set REVIEW_ACCOUNTS_EMAIL and REVIEW_ACCOUNTS_PASSWORD (as for "
              "seed_review_accounts).", file=sys.stderr)
        return 2
    journeys()
    width = max(len(r["path"]) for r in RESULTS)
    for r in RESULTS:
        mark = "ok " if r["ok"] else "BAD"
        print(f"{mark} {r['role']:<7} {r['method']:<5} {r['path']:<{width}} "
              f"[{r['build']}] {r['status']}  {r['note']}")
    bad = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(bad)} of {len(RESULTS)} steps answered as expected.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
