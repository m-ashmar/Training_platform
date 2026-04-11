---
trigger: always_on
---


You are enforcing strict data isolation.
Rules (MANDATORY):
	•	Authenticated responses must NEVER be globally cached
	•	Cache keys must include user identity for private data
	•	No cross-user data leakage allowed
REDIS SEGMENTATION:
	•	Separate logical DBs:
	◦	DB0: sessions
	◦	DB1: rate limiting
	◦	DB2: public cache
	◦	DB3: private cache
LOGGING:
	•	No sensitive data (PII, tokens, secrets) in logs
Reject any leakage risk.