Wallet Guide (Client, Trainer, Admin)

Scope
- This guide documents only the wallet features for Clients, Trainers, and Admins.
- Excludes Agent functionality (see AGENT_APP_INTEGRATION.md for that).
- Covers roles/permissions, flows, endpoints, request/response bodies, errors, production notes, audits, and a testing checklist.

Roles & Permissions
- Client
  - View own wallet balance and transactions.
  - Request a trainer (requires wallet balance ≥ trainer charge).
- Trainer
  - View own wallet balance and transactions.
  - See pending client requests, approve/reject.
  - Create routines and assign to approved clients (settles funds to trainer).
- Admin
  - Reversal of a transaction (append-only reversal entry).
  - Export audit logs, view suspicious activity alerts.

Wallet Model Concepts
- Wallet: One per user role; balances are updated only via API actions.
- Transaction: Append-only entries with unique reference_id; types include topup, transfer, reversal.
- Escrow: Platform wallet that temporarily holds funds when trainer approves a client request; on routine assignment, funds settle from escrow to trainer.

Core Flows
1) Client checks balance and transactions
   - GET /api/wallet/balance/
   - GET /api/wallet/transactions/
2) Client requests trainer
   - POST /api/auth/client/request-trainer/
   - Server validates client wallet balance ≥ trainer_hourly_rate; otherwise 402 Payment Required.
3) Trainer approves request (escrow hold)
   - POST /api/auth/trainer/respond-to-request/ with action=approve
   - Server moves trainer_hourly_rate from client wallet to escrow.
4) Trainer assigns routine (settlement)
   - POST /api/routine/routines/{id}/assign_to_client/
   - Server moves trainer_hourly_rate from escrow to trainer wallet.
5) Admin reversal (if needed)
   - POST /api/wallet/admin/reversal/
   - Server reverses a referenced transaction by creating a new reversal transaction (append-only).

Authentication & Transport
- All endpoints require JWT: Authorization: Bearer <access>.
- HTTPS/TLS only in production.

Endpoints & Request/Response

Client
1) Balance
- GET /api/wallet/balance/
- Headers: Authorization: Bearer <CLIENT_ACCESS>
- Response 200:
  {"id":4,"owner":313,"owner_type":"client","balance":"60.00","currency":"USD","created_at":"...","updated_at":"..."}

2) Transactions
- GET /api/wallet/transactions/
- Headers: Authorization: Bearer <CLIENT_ACCESS>
- Response 200: [ { "reference_id":"<uuid>", "amount":"40.00", "tx_type":"transfer|reversal|topup", "created_at":"...", "source_wallet":..., "destination_wallet":... }, ... ]

3) Request trainer
- POST /api/auth/client/request-trainer/
- Headers: Authorization: Bearer <CLIENT_ACCESS>, Content-Type: application/json
- Body: {"trainer_id": 314}
- Success 200: {"message":"Request sent to trainer <name>", "trainer_id":314, "status":"pending"}
- Insufficient funds 402: {"error":"Insufficient wallet balance to request this trainer","required":"40.00","balance":"35.00"}

Trainer
1) Balance
- Same as client but with trainer access token.

2) Pending requests
- GET /api/auth/trainer/pending-requests/
- Headers: Authorization: Bearer <TRAINER_ACCESS>
- Response 200:
  {"trainer_id":314,"pending_requests_count":1,"pending_requests":[{"request_id":79,"client_id":313,"client_name":"client_flow","requested_at":"...","status":"pending"}]}

3) Approve / Reject request
- POST /api/auth/trainer/respond-to-request/
- Headers: Authorization: Bearer <TRAINER_ACCESS>, Content-Type: application/json
- Approve Body: {"request_id":79,"action":"approve"}
- Approve 200: {"message":"Request from <client> approved successfully","client_id":313,"status":"approved"}
- If client balance changed and is insufficient (rare): 402 {"error":"Insufficient client wallet balance for trainer charge hold"}
- Reject Body: {"request_id":79,"action":"reject","reason":"..."}
- Reject 200: {"message":"Request from <client> rejected","client_id":313,"status":"rejected"}

4) Create routine
- POST /api/routine/routines/
- Headers: Authorization: Bearer <TRAINER_ACCESS>, Content-Type: application/json
- Body (minimal): {"name":"API Plan","days":3}
- Response 201/200: {"id":140, "name":"API Plan", ...}

5) Assign routine (settlement to trainer)
- POST /api/routine/routines/{id}/assign_to_client/
- Headers: Authorization: Bearer <TRAINER_ACCESS>, Content-Type: application/json
- Body: {"client_id":313}
- Response 200: {"message":"Routine '<name>' successfully assigned to <client>","routine_id":140,"client_id":313}

Admin
1) Reversal (append-only)
- POST /api/wallet/admin/reversal/
- Headers: Authorization: Bearer <ADMIN_ACCESS>, Content-Type: application/json
- Body: {"reference_id":"<original_tx_ref>","idempotency_key":"<uuid>","reason":"<text>"}
- Response 200: {"reference_id":"<new_reversal_ref>"}

2) Audit export
- GET /api/wallet/admin/audit/export/?start=<iso>&end=<iso>&agent_id=&user_id=
- Headers: Authorization: Bearer <ADMIN_ACCESS>
- Response 200: {"count":N,"results":[{"event_type":"wallet.transfer.success","actor_id":...,"path":"/api/...","payload":{...},"created_at":"..."}, ...]}

3) Suspicious activity alerts
- GET /api/wallet/admin/alerts/suspicious/?days=1
- Headers: Authorization: Bearer <ADMIN_ACCESS>
- Response 200: {"alerts":[{"type":"failed_auth_spike","actor_id":...,"count":...},{"type":"large_topups","actor_id":...,"total":...}]}

Error Handling (common)
- 401 Unauthorized: missing/expired JWT → refresh or re-login.
- 403 Forbidden: wrong role or not allowed → correct role/permissions.
- 402 Payment Required: client lacks balance for trainer charge (request or approval hold).
- 404 Not Found: invalid IDs.
- 409 Conflict: duplicate idempotency key (admin reversal uses idempotency; treat as previously processed).
- 429 Too Many Requests: throttling on money-moving endpoints in production → implement backoff.

Production Notes
- Always HTTPS; HSTS/CSP enabled server-side.
- JWT in Authorization header; do not store tokens in logs.
- Rate limits enforced on charging endpoints (production), relaxed in development.
- All balance changes occur in atomic transactions with row locking; UI should refresh balance after success.

Auditing & Compliance
- Every wallet-related operation is recorded to WalletAuditLog (event_type, actor, path, payload, created_at).
- Admin export endpoint supports time and actor filters.
- Suspicious activity endpoint surfaces simple heuristics (e.g., spikes, large totals).

Testing Checklist (Client/Trainer/Admin)
Client
- [ ] Login; GET /api/wallet/balance/ returns current balance.
- [ ] If balance < trainer charge, POST /api/auth/client/request-trainer/ returns 402 with required and balance fields.
- [ ] With sufficient balance, request returns 200 and status=pending.

Trainer
- [ ] Pending requests list contains the client request.
- [ ] Approving request returns 200 and reduces client balance by trainer_hourly_rate (escrow hold). Trainer balance unchanged.
- [ ] Create routine returns id.
- [ ] Assign routine to client returns 200 and increases trainer balance by trainer_hourly_rate (settlement from escrow). Client balance unchanged post-approval.

Admin
- [ ] Reversal: POST /api/wallet/admin/reversal/ returns new reference_id; balances reflect the reversal.
- [ ] Audit export returns recent wallet events filtered by date.
- [ ] Suspicious alerts returns expected test alerts after simulated activity.

Operational Guidance
- Frontend should always confirm balances via GET /api/wallet/balance/ after any successful money-moving action.
- Display transaction history to improve user trust and support investigation.
- Handle all error shapes above with clear user messages and retry/backoff strategies where appropriate.


