---
trigger: always_on
---


You are enforcing financial-grade security for wallet operations.
Rules (MANDATORY):
	•	Dev-mode must NEVER exist in production execution paths
	•	No config flag may disable financial validation in production
	•	All transactions must be validated server-side
REQUIRED VALIDATIONS:
	•	HMAC signature verification
	•	Timestamp freshness (prevent replay attacks)
	•	IP allowlist enforcement
	•	API key validation
API KEY SECURITY:
	•	Keys must NOT be stored in plaintext
	•	Use hashing or secure derivation for storage
AUDIT LOGGING:
	•	All financial logs must be tamper-evident
	•	Each log must include a hash chain of previous entry
	•	Logs must be append-only
Reject any exploitable path.