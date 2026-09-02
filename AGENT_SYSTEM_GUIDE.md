# Agent System — Complete Guide (A → Z)

Everything about the wallet **agent** role: what it is, how to create one, how to set
it up, and how to top up user balances. Reflects the post-audit state (Phase 2).

Base URL (prod): `https://training-platform-api.fly.dev`
All wallet routes are under `/api/wallet/`. All auth routes under `/api/auth/`.

---

## 1. What an agent is

An **agent** is a trusted cash-in operator. In the real world the agent collects
money (cash, local transfer, etc.) from a user, then credits that user's in-app
wallet — a **top-up**.

**Launch model = prepaid / trusted.** A top-up *mints* balance into the user's wallet
from the system; the agent is **not** charged a pre-funded balance yet. The safety
control is the **per-agent daily/monthly cap** (default **$200/day**). When you later
switch to *pre-funded*, agents will buy balance first and top-ups will debit the
agent's own wallet — that's a localized future change; nothing in this guide changes.

**Roles recap:** `client` (end user), `trainer`, `agent` (this guide), `admin` (superuser).

---

## 2. Data model (what backs an agent)

| Model | Purpose |
|---|---|
| `CustomUser` (`user_type="agent"`) | The agent's login account. |
| `AgentProfile` | Per-agent config: `status`, `daily_limit`, `monthly_limit`, `wallet_type`, `ip_allowlist`. Auto-created with default caps. |
| `AgentAPIKey` | Optional HMAC credential for server-to-server top-ups. Raw secret stored **encrypted** (`secret_ciphertext`). Not needed for the mobile flow. |
| `Wallet` | Balance holder. Users (client/trainer) get one automatically; top-ups credit the **client's** wallet. |
| `Transaction` | Immutable record of every topup/transfer/reversal (`reference_id`). |
| `IdempotencyKey` | Guarantees a top-up is processed exactly once. |
| `WalletAuditLog` | Append-only, hash-chained audit trail of every wallet action. |

---

## 3. How to create an agent

Self-registration as `agent` is **blocked** (Phase 1 hardening) — agents are privileged.
Only a **superuser** can create one. Two ways:

### Option A — Django admin (recommended, simplest)
1. Log in to the admin at `/dj-admin/` as a superuser.
2. **Users → Add user.** Set:
   - `email`, `username`, `password`
   - **`user_type` = `agent`** (set this on the creation form, not after)
   - `is_active` = **True** (so the agent can log in without the OTP step)
3. Save. A `post_save` signal auto-creates the `AgentProfile` with the default caps
   (`$200/day`, `$5000/month`, `status=active`, `wallet_type=prepaid`).

### Option B — API, as an authenticated superuser
```bash
curl -X POST https://.../api/auth/register/ \
  -H "Authorization: Bearer <SUPERUSER_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"username":"agent_ali","email":"ali@ex.com",
       "password1":"StrongPass123!","password2":"StrongPass123!",
       "phone_number":"+1234567890","user_type":"agent"}'
```
This path creates the user **inactive** and sends an OTP to the agent's email; the
agent must verify via `/api/auth/verify-otp/` before logging in. (Admin/Option A avoids
the OTP step by setting `is_active=True` directly.)

> **Robustness note:** even if the profile wasn't created at signup, the **first** agent
> API call lazily creates the `AgentProfile` with default caps. An agent is never stuck
> without a profile.

---

## 4. Setting up an agent

### Log in (get a JWT)
```bash
curl -X POST https://.../api/auth/token/ \
  -H "Content-Type: application/json" \
  -d '{"email":"ali@ex.com","password":"StrongPass123!"}'
# → {"access":"<JWT>", "refresh":"...", "user":{...,"user_type":"agent"}}
```
Use `Authorization: Bearer <access>` on every call below.

### Adjust caps / status / IP allowlist (superuser, in `/dj-admin/`)
Open **Agent profiles → the agent** and edit:

| Field | Meaning |
|---|---|
| `status` | `active` / `suspended` / `banned`. Only `active` can top up. |
| `daily_limit` | Max total top-ups per calendar day. **`0` = no top-ups (fail-closed)** — raise it to allow more. |
| `monthly_limit` | Max total top-ups per calendar month. |
| `ip_allowlist` | Optional list of allowed IPs for the **HMAC** flow (empty = any). |

Defaults come from settings and are env-tunable:
`AGENT_DEFAULT_DAILY_LIMIT` (200), `AGENT_DEFAULT_MONTHLY_LIMIT` (5000).

---

## 5. The two top-up flows

| Flow | Endpoint | Auth | Use when |
|---|---|---|---|
| **Mobile proxy (recommended)** | `POST /api/wallet/agent/topup/proxy` | JWT only | The agent uses the mobile app. Signing secret stays server-side. |
| **Server-to-server HMAC** | `POST /api/wallet/agent/topup/` | JWT **+** HMAC header | A backend integration that holds its own API secret. |

> The mobile proxy is JWT-only **by design** — no signing secret is ever placed on the
> phone. It's the intended path for human agents. HMAC is only for machine integrations.
> ⚠️ The proxy route has **no trailing slash**: `/api/wallet/agent/topup/proxy`.

---

## 6. Top up a user's balance — mobile flow (A→Z)

**Step 1.** Agent logs in (§4) → gets `access` token.

**Step 2.** POST the top-up:
```bash
curl -X POST https://.../api/wallet/agent/topup/proxy \
  -H "Authorization: Bearer <AGENT_ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
        "client_identifier": "client@ex.com",   // client user id OR email
        "amount": "50.00",
        "idempotency_key": "b1f2...-unique-per-attempt"
      }'
```

**Step 3.** Success:
```json
{ "reference_id": "9a7c...", "balance": "50.00" }   // client's NEW balance
```

**What the server enforces (in order):** JWT is a valid `agent` → agent `status=active`
→ target exists and is a `client` → **daily/monthly cap** not exceeded → idempotency key
reserved → funds credited atomically → audit logged.

**Idempotency:** reuse the **same** `idempotency_key` to safely retry a request whose
response you didn't receive — you'll get the original result back, not a double credit.
Use a **new** key for each genuinely new top-up.

---

## 7. Top up a user's balance — server-to-server HMAC flow

For backend integrations only. Requires an API key.

**Step 1 — create an API key (once, as the agent):**
```bash
curl -X POST https://.../api/wallet/agent/apikey/create/ \
  -H "Authorization: Bearer <AGENT_ACCESS_TOKEN>"
# → {"key_id":"ab12...","secret":"<64-hex SHOWN ONCE>"}
```
Store the `secret` securely on your server — it is shown **once** and kept only
encrypted server-side. (`/apikey/status/` tells you if a key exists; `/apikey/ensure/`
creates one without returning the secret.)

**Step 2 — sign and send the top-up:**
- Build the message string: `client_identifier|amount|timestamp|idempotency_key`
- `signature = HMAC_SHA256(secret, message)` (hex)
- `timestamp` = current unix seconds (must be within **60s** of server time)
```bash
curl -X POST https://.../api/wallet/agent/topup/ \
  -H "Authorization: Bearer <AGENT_ACCESS_TOKEN>" \
  -H "X-Agent-Auth: AgentAuth key_id=ab12...,signature=<hex>,timestamp=<unix>" \
  -H "Content-Type: application/json" \
  -d '{"client_identifier":"client@ex.com","amount":"50.00",
       "timestamp":<unix>,"idempotency_key":"unique","signature":"<hex>"}'
```
The server verifies the signature against the **decrypted** secret, checks timestamp
freshness, optional IP allowlist, then applies the same validation/caps/idempotency as
the mobile flow.

---

## 8. Full endpoint reference (`/api/wallet/`)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/balance/` | any user (JWT) | Caller's own wallet balance |
| GET | `/transactions/` | any user (JWT) | Caller's last 200 transactions |
| POST | `/agent/topup/proxy` | agent (JWT) | **Mobile top-up** (no trailing slash) |
| POST | `/agent/topup/` | agent (JWT + HMAC) | Server-to-server top-up |
| POST | `/agent/apikey/create/` | agent (JWT) | Create key; returns secret once |
| GET | `/agent/apikey/status/` | agent (JWT) | `{"has_active": true/false}` |
| POST | `/agent/apikey/ensure/` | agent (JWT) | Create if missing (no secret returned) |
| POST | `/client/transfer/` | client (JWT) | Client → trainer transfer |
| POST | `/admin/reversal/` | superuser (JWT) | Reverse a transaction (once) |
| GET | `/admin/audit/export/` | superuser (JWT) | Export audit log; `?verify=1` checks chain |
| GET | `/admin/alerts/suspicious/` | superuser (JWT) | Heuristic alerts; `?days=N` |

---

## 9. Limits, caps & how to change them

- Caps are enforced **fail-closed**: a limit of `0` means **no top-ups**, not unlimited.
- Change a single agent's caps in `/dj-admin/` → Agent profiles.
- Change the defaults for *new* agents via env: `AGENT_DEFAULT_DAILY_LIMIT`,
  `AGENT_DEFAULT_MONTHLY_LIMIT`.
- Totals are summed per **calendar day** and **calendar month** across the agent's
  top-ups; a new top-up is blocked if it would push the running total over the cap.

---

## 10. Related money flows

- **Client → trainer transfer** (`/client/transfer/`): a client pays their trainer from
  their own wallet. Requires sufficient balance; idempotent.
- **Admin reversal** (`/admin/reversal/`): a superuser reverses a transaction by its
  `reference_id`. Each transaction can be reversed **once**.
- **Automatic subscription payout:** when a subscription `Payment` completes, the system
  auto-tops-up the client and transfers to their assigned trainer (idempotent, via signal).

---

## 11. Security & audit

- Every wallet action writes an append-only, hash-chained `WalletAuditLog` entry.
- Verify chain integrity: `GET /api/wallet/admin/audit/export/?verify=1` →
  `{"chain_valid": true, "first_tampered_id": null, ...}`.
- Agent API secrets are stored **encrypted at rest**; the raw secret is shown only once
  at creation and never logged.
- Suspicious-activity heuristics: `GET /api/wallet/admin/alerts/suspicious/?days=1`
  (failed-auth spikes, large/rapid top-ups).

---

## 12. Troubleshooting

| Symptom | Cause | Resolution |
|---|---|---|
| `403 Agent not active` | `AgentProfile.status` ≠ `active` | Set status to `active` in admin |
| `429 Daily/Monthly limit exceeded` | Cap reached (or cap is `0`) | Raise the agent's cap in admin |
| `400 Target must be a client` | `client_identifier` isn't a client user | Use a valid client id/email |
| `404` on target | No user with that id/email | Check the identifier |
| `409 Duplicate request` | Same `idempotency_key` still processing | Retry the *same* key to fetch result, or use a new key for a new top-up |
| `401 Signature verification failed` (HMAC) | Wrong secret, stale timestamp, or key issued before the audit (NULL ciphertext) | Re-create the API key via `/apikey/create/` |
| `402` on client→trainer | Insufficient wallet balance | Top up the client first |

---

## 13. Notes & future work

- **Existing agents from before the audit** were reset by the fail-closed change: their
  `daily_limit=0` blocks top-ups until you set a cap, and any old API keys must be
  re-created. (No real agents existed pre-production, so this is a non-issue now.)
- **Rate limiting:** agents currently inherit the anonymous global tier (~100 req/hr in
  prod) since there's no dedicated `agent` tier. Harmless at the $200/day cap, but worth
  adding an `agent` tier before scaling agent volume.
- **Pre-funded switch (future):** give agents their own funded `Wallet`, and change the
  top-up path to debit the agent instead of minting from the system. Localized change in
  `move_funds_atomic` + the two top-up views.

## Apply before onboarding real agents
1. Run `manage.py migrate wallet` (adds `secret_ciphertext`).
2. Confirm default caps suit your launch ($200/day) or adjust env/admin.
