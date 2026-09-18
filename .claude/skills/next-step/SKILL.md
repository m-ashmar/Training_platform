---
name: next-step
description: Build the next step of the payments and store-compliance plan, end to end, in this one session. Use when the owner types /next-step (optionally with a step number) or says "do the next step".
---

# Next step of PAYMENTS_COMPLIANCE_PLAN.md

1. Read `PAYMENTS_COMPLIANCE_PLAN.md`. Take the step number given as the argument; with no
   argument take the first step whose status is NEXT, otherwise the first TODO. Read the
   spec section only if the plan does not say enough (the link is at the top of the plan;
   read it with the docs connector, never web-fetch).
2. Say in three lines what you will build, which files it touches, and what "done" means.
   If the step is too large for one session (step 5 is), build the first coherent slice,
   and say which slice.
3. Check out the branch the plan names. Do not push; do not merge into `noti-system`.
4. Build it with tests written from the behaviour the owner is owed. Follow `CLAUDE.md`:
   `require_purchase_allowed` first on any money endpoint, `money_type` on every
   `move_funds_atomic`, additive API changes, new routes into `API_CONTRACT.md`, anything
   the app must know into `FRONTEND_HANDOFF.md` §3.
5. Run the step's own tests, then the full gate with `--create-db` in the background to a
   log file in the scratchpad. Do not edit `.py` files while it runs. Read only failures.
6. When green: update the plan (status, what exists, what is left, new traps), commit on
   the branch with the repo's message style, update the project memory file
   `project-payments-compliance-spec.md` with the new HEAD and state.
7. Finish with: what changed, what was run and the result, decisions the owner owes, and
   the line to paste for the next step. Then stop — the next step is a new session.
