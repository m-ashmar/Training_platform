---
trigger: always_on
---

ou are enforcing enterprise-grade secrets management.
Rules (MANDATORY):
	•	NO secrets in source code
	•	NO .env usage in production
	•	Environment variables alone are NOT sufficient for production secrets
	•	Secrets MUST be loaded from a managed secrets service (AWS Secrets Manager, Vault, etc.)
REQUIREMENTS:
	•	Secrets must be dynamically fetched at runtime
	•	Secrets must be rotatable without redeploy
	•	Missing secrets must crash application startup
	•	No fallback or default values allowed
APPLIES TO:
	•	Django SECRET_KEY
	•	JWT keys
	•	Database credentials
	•	Firebase credentials
	•	API keys (OpenAI, etc.)
Reject any insecure handling.
