Wallet System Implementation Guide

Overview
- New `wallet` app providing secure, auditable wallets for clients, trainers, and agents.
- Atomic transactions, idempotency, HMAC agent auth, IP allowlists, throttling, and audit logs.

Models
- Wallet(owner, owner_type, balance, currency)
- Transaction(reference_id, tx_type, actor, source_wallet, destination_wallet, amount, metadata, created_at)
- IdempotencyKey(key, request_hash, created_by, processed, response_snapshot)
- WalletAuditLog(event_type, actor, request_id, ip_address, user_agent, path, payload)
- AgentProfile(user, wallet_type, daily_limit, monthly_limit, status, ip_allowlist)
- AgentAPIKey(agent, key_id, hashed_key, is_active)

Security
- Agent requests must include `X-AGENT-AUTH: AgentAuth key_id=...,signature=...,timestamp=...`.
- Signature = HMAC-SHA256(secret, message) where message = `client_identifier|amount|timestamp|idempotency_key`.
- Freshness window: 60s.
- Idempotency enforced per `idempotency_key`.
- Charging endpoints throttled (`charging: 10/min`).
- Full audit log for balance/transactions/topups/transfers/reversals.

Endpoints
- GET /api/wallet/balance/ → Wallet details for current user.
- GET /api/wallet/transactions/ → Recent transactions (<=200) for current user.
- POST /api/wallet/agent/topup/ (Agent only)
  body: {client_identifier, amount, idempotency_key, timestamp, signature}
- POST /api/wallet/client/transfer/ (Client only)
  body: {trainer_id, amount, idempotency_key}
- POST /api/wallet/admin/reversal/ (Admin only)
  body: {reference_id, reason?, idempotency_key}

Admin & Ops
- Admin can manage `AgentProfile`, `AgentAPIKey`, view `Transaction` and `WalletAuditLog` in Django admin.
- Signals create wallets for clients/trainers and agent profile scaffold for agents.

Next Steps for Production
- Store agent secrets securely and hash with pepper; rotate regularly.
- Move emails, domains, Redis, DB, Sentry, and DEBUG off to env vars.
- Replace file logs with JSON stdout.
- Consider moving MEDIA to S3 and set HTTPS/HSTS cookies.

Testing
- Add integration tests that simulate: agent top-up → client transfer to trainer → admin reversal.


