---
trigger: always_on
---


You are enforcing production deployment security.
Runtime MUST fail if:
	•	DEBUG=True in production
	•	Required secrets missing
	•	Unsafe flags enabled (e.g., WALLET_DEV_MODE)
SECURITY HEADERS:
	•	HSTS must be enabled
	•	SSL redirect must be enforced
	•	Cookies must be Secure + HTTPOnly
CI/CD REQUIREMENTS:
	•	Bandit
	•	Safety
	•	Semgrep
	•	Secret scanning (gitleaks or equivalent)
OBSERVABILITY:
	•	Monitor:
	◦	OTP failures
	◦	auth anomalies
	◦	wallet transaction spikes
	◦	500 errors
Reject any unsafe deployment configuration.