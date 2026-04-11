---
trigger: always_on
---


You are enforcing secure API and authentication design.
Rules (MANDATORY):
	•	No parallel authentication systems without strict synchronization
	•	dj-rest-auth or similar must NOT bypass OTP verification
	•	All endpoints must define permission_classes explicitly
	•	JWT must use asymmetric signing (RS256)
	•	No sensitive endpoint allows anonymous access
	•	Email/OTP flows must prevent enumeration
OTP SECURITY:
	•	OTP must be cryptographically generated (secrets module)
	•	OTP must be stored as a hash (never plaintext)
	•	OTP validation must use constant-time comparison (compare_digest)
	•	OTP must expire
	•	OTP must have attempt limits and lockout after 5 failures
Reject any auth bypass or weak OTP system.
