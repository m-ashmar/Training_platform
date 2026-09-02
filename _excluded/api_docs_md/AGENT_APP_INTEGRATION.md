Agent App Integration Guide (Production)

Scope: A dedicated mobile app for Agents only. No self-registration. Agent logs in, manages API keys, performs secure client wallet top-ups via HMAC-signed requests with idempotency, and can review recent activity locally. This guide breaks down every step for the mobile team in production conditions.

Security-first assumptions
- All traffic over HTTPS/TLS.
- JWT in Authorization header for every API call after login.
- API key secrets never leave the device and are never logged or backed up.
- Agent devices must have reasonably correct time (timestamp freshness window: 60 seconds).
- In production, HMAC signature + idempotency + IP allowlist are enforced.

API base and endpoints
- Base URL: provided per environment (staging/prod). Examples below use http://127.0.0.1:8000 for clarity.
- Auth: POST /api/auth/token/
- Agent API key: POST /api/wallet/agent/apikey/create/
- Top-up: POST /api/wallet/agent/topup/
- Optional admin-only (not in agent app): /api/wallet/admin/audit/export/, /api/wallet/admin/alerts/suspicious/

App screens and flows
1) Login (Agent only)
   - Input: email, password
   - Request: POST /api/auth/token/
     - Body: {"email":"agent@example.com","password":"<password>"}
     - Response: {"access":"<jwt>","refresh":"<token>","user":{"id":...,"user_type":"agent"}}
   - Store access token in memory; if you implement refresh, store refresh token in secure storage.
   - On 401 responses elsewhere, either refresh or force a relogin.

2) API Keys (create/rotate)
   - Purpose: Generate an API key pair that the device will use for HMAC signing of top-ups.
   - Request: POST /api/wallet/agent/apikey/create/
     - Headers: Authorization: Bearer <access>
     - Body: {"name":"<device label>"}
     - Response (secret shown once): {"key_id":"<16-hex>","secret":"<high-entropy-secret>"}
   - Device handling:
     - Persist key_id using normal storage.
     - Persist secret using secure storage (Keychain/Keystore), protected by device lock/biometrics; hide by default, reveal-on-touch behind biometric/PIN.
     - Never log or screenshot the secret; do not sync to cloud backups if possible.
   - Rotation:
     - Create a new key → switch app to use it → request admin to deactivate old key server-side.

3) Top-up (core money movement)
   - Inputs on screen:
     - client_identifier: email (preferred) or numeric user_id of the client to credit.
     - amount: string-decimal with exactly two decimals (e.g., "100.00").
   - Generated per submission:
     - idempotency_key: UUIDv4 (persist locally with the submitted record for retries).
     - timestamp: Unix seconds (int). Must be fresh (≤ 60s) at server receipt.
     - signature: HMAC-SHA256(secret, message).hex()
   - Message to sign (exact, no spaces/newlines):
     client_identifier|amount|timestamp|idempotency_key
     Example: "client_flow@example.com|100.00|1730000000|550e8400-e29b-41d4-a716-446655440000"
   - HTTP request:
     - Endpoint: POST /api/wallet/agent/topup/
     - Headers:
       - Authorization: Bearer <access>
       - X-AGENT-AUTH: AgentAuth key_id=<KEY_ID>,signature=<SIG>,timestamp=<TS>
       - Content-Type: application/json
     - Body:
       {"client_identifier":"client_flow@example.com","amount":"100.00","idempotency_key":"<uuid>","timestamp":1730000000,"signature":"<hex>"}
   - Success response:
       {"reference_id":"<uuid>","balance":"<client_new_balance>"}
   - UI behavior:
     - Show spinner, then show new client balance and reference_id on success.
     - Persist a local ledger entry: {idempotency_key, client_identifier, amount, timestamp, reference_id}.
   - Do not regenerate idempotency_key on retry; reuse it to guarantee at-most-once effects.

Cryptography 101 (for the team)
- HMAC-SHA256: A keyed hash that proves the request originated from a holder of the secret.
  - Inputs: secret (from API key creation), message (the canonical concatenation).
  - Output: hex string (64 chars) sent as signature.
  - Common mistakes:
    - Extra whitespace/newlines in message.
    - Using a different amount format (must be two decimal places as sent in body).
    - Using milliseconds for timestamp instead of seconds.
    - Encoding mismatch (ensure UTF-8 for message and secret bytes).
- Timestamp freshness (≤ 60 seconds): If too old, server rejects the request (Invalid agent auth). Ensure device time is synced.
- Idempotency: A unique key per logical request lets the server return the first result on repeats (prevents double-credit).

Production constraints to observe in the app
- IP allowlist: In production, top-ups from an IP not on the agent’s allowlist are rejected (403). If agents are mobile, use a corporate VPN/static egress IP.
- Rate limiting: Top-up endpoints are throttled. Implement a 1–2s client-side minimum interval and exponential backoff on 429.
- No secrets in logs: Redact Authorization, signature, and secret from any telemetry.
- Biometric/PIN gate to reveal API secret. Disable screenshots on the API key screen (platform capability).
- Do not cache client balances locally; trust server responses and on-demand queries.

Error handling guide (map to UX)
- 401 Unauthorized: JWT expired/invalid → refresh or send user to Login.
- 403 Forbidden: IP not allowed or agent inactive → show an instruction to contact Admin / use approved network.
- 400 Bad Request: signature mismatch or bad payload → show “Signature invalid or request malformed.” Offer to re-sign with a new timestamp.
- 409 Conflict: duplicate idempotency key → treat as success; show previously processed result from server.
- 429 Too Many Requests: rate limit → backoff, disable submit until cooldown ends.
- Network unknown: request may have reached server → ask to retry using the SAME idempotency_key.

Local activity view (My Top-ups)
- Current API does not expose a dedicated "list my top-ups" for agents.
- MVP approach: maintain a local ledger of submitted top-ups with their idempotency_key, reference_id (when known), and timestamps. Display this list and allow copy of reference_id.
- Future (server): add endpoint to list transactions filtered by actor=request.user for full parity.

Implementation checklist per screen
Login
- [ ] Validate email/password.
- [ ] POST /api/auth/token/; store access (memory) and optionally refresh (secure storage).
- [ ] On 401 elsewhere, refresh or re-login.

API Keys
- [ ] POST /api/wallet/agent/apikey/create/ with a device label.
- [ ] Store key_id (normal storage) and secret (secure storage). Mask secret by default; reveal behind biometric/PIN.
- [ ] Provide rotate flow: create new key → switch default → notify admin to disable prior key.

Top-up
- [ ] Input: client email or ID; amount string with two decimals.
- [ ] Generate idempotency_key (UUIDv4) and timestamp (int seconds).
- [ ] Construct exact message: client|amount|timestamp|idempotency_key (no spaces/newlines).
- [ ] Compute signature = HMAC_SHA256(secret, message).hex().
- [ ] POST with headers (Authorization, X-AGENT-AUTH) and JSON body.
- [ ] On success, display reference_id and new client balance; persist local ledger record.
- [ ] On retry, reuse the same idempotency_key.

Dashboard
- [ ] Aggregate local ledger to show today/month totals.
- [ ] Show guidance if 403 (IP) or frequent 429 appear.

Settings
- [ ] Reveal/copy key_id; reveal secret gated by biometric/PIN.
- [ ] Logout clears JWT; keep secret unless performing full device reprovisioning.

Request/response snippets (copy-ready)
Login (Agent)
POST /api/auth/token/
Headers: Content-Type: application/json
Body:
{"email":"agent@example.com","password":"<password>"}
Response (200):
{"access":"<jwt>","refresh":"<token>","user":{"id":312,"email":"agent@example.com","user_type":"agent"}}

Create API Key
POST /api/wallet/agent/apikey/create/
Headers: Authorization: Bearer <access>, Content-Type: application/json
Body:
{"name":"my-device-key"}
Response (201):
{"key_id":"1d93cb5e0c004b94","secret":"<STORE_THIS_SECURELY>"}

Top-up (Production)
Compute: message = "client_identifier|amount|timestamp|idempotency_key"
signature = HMAC_SHA256(secret, message).hex()
POST /api/wallet/agent/topup/
Headers:
- Authorization: Bearer <access>
- X-AGENT-AUTH: AgentAuth key_id=<KEY_ID>,signature=<SIG>,timestamp=<TS>
- Content-Type: application/json
Body:
{"client_identifier":"client_flow@example.com","amount":"100.00","idempotency_key":"550e8400-e29b-41d4-a716-446655440000","timestamp":1730000000,"signature":"<hex>"}
Response (200):
{"reference_id":"69cd7a6a-9750-43e2-adc9-46d735d15605","balance":"100.00"}

What to be careful about (common pitfalls)
- Amount formatting: must be string with two decimals; the same text must be used in both message and JSON body.
- Message separators: exactly one pipe | between fields; no trailing separators or whitespace.
- Encoding: compute HMAC over UTF-8 bytes of the message; secret must be the exact bytes of the stored secret.
- Timestamp: seconds, not milliseconds. Recompute signature if you update timestamp.
- Idempotency: never regenerate the key for the same logical top-up; on network errors, retry with the same key.
- Secrets: never log, never embed in crash reports, never capture in screenshots; protect behind biometric/PIN.
- IP allowlist: 403 means your current egress IP is not allowed—have admin add it or use approved VPN.
- Backoff on 429: do not spam the endpoint; implement exponential backoff and UI cooldown.

End-to-end testing checklist (hand to QA/mobile dev)
Environment setup
- [ ] Valid agent account provisioned by admin.
- [ ] Agent device time is correct (automatic time enabled).
- [ ] Agent’s current network egress IP is allowlisted (prod) or using corporate VPN.

Happy path
- [ ] Login succeeds; app stores JWT.
- [ ] Create API key; app stores key_id and secret securely (secret masked by default).
- [ ] Perform top-up for a known client: amount "100.00"; idempotency_key = UUID; timestamp fresh.
- [ ] Server returns 200 with reference_id and updated client balance; app shows confirmation and logs local ledger.

Retries and idempotency
- [ ] With the SAME idempotency_key, resend the top-up → server returns previous result (no double credit) or 200 with same reference_id.

Security enforcement
- [ ] Use a deliberately wrong signature → server returns 401 Invalid agent auth.
- [ ] Use a stale timestamp (>60s) → server returns 401 Invalid agent auth.
- [ ] Switch to a non-allowlisted IP (prod) → server returns 403 IP not allowed.

Limits and throttling
- [ ] Rapid submits → observe 429; app backs off and disables submit briefly.
- [ ] Exceed configured daily/monthly limit (if admin set) → server returns 429 with limit context.

UX hardening
- [ ] Secret reveal requires biometric/PIN; screenshots blocked on the secret screen.
- [ ] App redacts secrets, JWTs, and signatures from logs/analytics.
- [ ] On network timeout, app offers retry using the SAME idempotency_key.

Go-live readiness
- [ ] All above checks pass on staging with production-like settings (WALLET_DEV_MODE=False).
- [ ] Admin has confirmed agent IP ranges and limits.
- [ ] Incident runbook: how to handle lost device (revoke key), clock drift issues, repeated 401/403/429.


