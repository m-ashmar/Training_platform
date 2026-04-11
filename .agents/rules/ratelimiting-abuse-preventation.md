---
trigger: always_on
---


You are enforcing defense-in-depth rate limiting.
Rules (MANDATORY):
Rate limiting must exist at THREE layers:
	1	Edge (CDN/WAF such as Cloudflare)
	2	Infrastructure (NGINX limit_req or equivalent)
	3	Application (django-ratelimit with Redis backend)
OTP / AUTH RULES:
	•	OTP verification must have strict attempt limits
	•	Lock account/IP after 5 failed attempts
	•	Login/password reset must be rate-limited
Reject any single-layer rate limiting system.