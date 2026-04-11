---
trigger: always_on
---


You are enforcing Django production-grade security architecture.
Rules (MANDATORY):
	•	Settings must be split into base.py, local.py, production.py
	•	DEBUG must NEVER be True in production
	•	Application must crash if misconfigured in production
	•	SecurityMiddleware must be FIRST in MIDDLEWARE
	•	No business logic inside settings files
	•	request.user must never be trusted without authentication checks
	•	All endpoints must explicitly define permission_classes
	•	Avoid global mutable state or thread-unsafe patterns
	•	Use Django-native patterns correctly (middleware, permissions, signals)
SECRET KEY MANAGEMENT:
	•	SECRET_KEY must be loaded from secure source
	•	SECRET_KEY must support rotation using SECRET_KEY_FALLBACKS
	•	Old keys must remain temporarily valid for session continuity
Reject any violation.
