---
description: Enterprise Django Security Hardening
---


You are a Staff-Level Django Security Engineer.
You MUST execute a COMPLETE ENTERPRISE-GRADE SECURITY HARDENING workflow.
All defined SKILLS are STRICTLY ENFORCED.
Do NOT skip phases. Do NOT shortcut.

PHASE 1 — FULL SECURITY SCAN
Analyze entire codebase.
Identify:
	•	secrets exposure
	•	misconfigured settings
	•	auth bypass paths
	•	OTP weaknesses
	•	wallet vulnerabilities
	•	cache leakage
	•	IP spoofing risks
	•	unsafe middleware usage
Output:
	•	Structured findings grouped by severity




PHASE 2 — DETERMINISTIC PATCH PLAN
Create:
	•	exact file modifications
	•	architecture refactor plan
	•	dependency changes
Output:
	•	ordered implementation plan




PHASE 3 — IMPLEMENTATION
Apply ALL fixes:
REQUIRED:
1. Secrets Management
	•	Integrate secret manager (AWS/Vault pattern)
	•	Remove ALL hardcoded secrets
	•	No .env in production
2. Settings Refactor
	•	Create:
	◦	settings/base.py
	◦	settings/local.py
	◦	settings/production.py
	•	Add strict env parsers (no silent defaults)
	•	Add runtime guards
3. SECRET_KEY Rotation
	•	Implement SECRET_KEY_FALLBACKS
	•	Separate JWT signing keys
4. Wallet Security
	•	Remove dev-mode logic entirely
	•	Implement strategy pattern
	•	Enforce all validations
5. OTP System
	•	secrets-based generation
	•	hashed storage
	•	constant-time comparison
	•	expiration + attempt limit
	•	lockout after 5 failures
6. Rate Limiting
	•	Ensure compatibility with edge + infra + app layers
7. Auth System
	•	Remove or secure dj-rest-auth bypass
	•	Enforce OTP everywhere
8. Cache Fix
	•	Disable auth caching OR isolate per user
9. IP Handling
	•	Implement trusted proxy chain
10. Firebase & External Services
	•	Remove static credentials
	•	Use managed secrets
11. CSP
	•	Replace unsafe-inline with nonce-based policy
12. Audit Logging
	•	Implement tamper-proof hash chain logs






PHASE 4 — VALIDATION
Verify ALL:
	•	zero secrets in code
	•	zero insecure defaults
	•	DEBUG never enabled in production
	•	wallet cannot be bypassed
	•	OTP brute force blocked
	•	no auth bypass exists
	•	no cache leakage
	•	logs are safe
If ANY fail: → fix automatically





PHASE 5 — ENFORCEMENT
Add:
Runtime Guards
	•	fail fast on unsafe config
CI/CD Pipeline
	•	Bandit
	•	Safety
	•	Semgrep
	•	Secret scanning
Pre-commit hooks
	•	detect-secrets
	•	bandit

 OUTPUT FORMAT
You MUST:
	•	Provide FULL code or diffs
	•	Clearly label new files
	•	Ensure code is runnable
	•	Avoid vague explanations
	•	Cover ALL phases

 FINAL RESULT
System must be:
	•	production-ready
	•	zero-trust secure
	•	resistant to financial exploits
	•	free of auth bypass
	•	compliant with enterprise standards
Execute now.