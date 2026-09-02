# Fable 5 Pre-Production Audit — Query Guide

## How to Run the Audit

### Step 1: Access Method (Claude Pro)

Go to **claude.ai → New Project** (not a new chat — use Projects).

1. Create a new Project named: `Training Platform — Pre-Production Audit`
2. In **Project Instructions**, paste the System Prompt below (Section A)
3. In the first message, upload `fable_audit_bundle.txt` and paste the Audit Query (Section B)

> **Why Projects?** Project files and instructions are **cached** by Anthropic.  
> Follow-up questions cost ~90% less than the first turn (~$0.33 vs ~$3.26 per API turn).  
> On Pro plan this is usage-limited but the caching still applies within the session.

---

### Step 2: Upload the Bundle

File to upload: `fable_audit_bundle.txt` (1.27MB, ~325K tokens)

The bundle is **pre-sorted by audit priority**:
- **TIER 1** opens with `SYSTEM_OVERVIEW.md` then security/auth/wallet — the most critical surfaces
- **TIER 2** covers diet AI pipeline, routine, subscription gateways
- **TIER 3** covers social, AI assistant, achievements, notifications, infra

---

## SECTION A: Project System Prompt (paste into Project Instructions)

```
You are performing a final pre-production security and architecture audit of a 
Django 5.1 REST + WebSocket backend called the Training Platform.

Your role: senior security engineer, Django architect, and code reviewer.
Your output must be: structured, specific, actionable, and prioritized by severity.

CONTEXT:
- The codebase bundle is attached. It contains 143 source files organized into 3 tiers.
- SYSTEM_OVERVIEW.md at the top of the bundle is your architecture map. Read it first.
- Production target: PostgreSQL, Redis (6 DBs), Celery, Daphne ASGI, Fly.io
- Auth: RS256 JWT, custom OTP, 4 user roles (client, trainer, agent, admin)
- Financial: internal wallet system with escrow, audit logs, agent API keys
- AI: OpenAI GPT-4o-mini for diet planning and conversational coach
- Payments: Stripe + 3 regional gateways (Baraka, Bemo, Syriatel)

MANDATORY AUDIT SCOPE:
1. Security vulnerabilities (auth bypass, injection, IDOR, privilege escalation)
2. Financial logic flaws (wallet, escrow, transactions, idempotency)
3. OTP/JWT implementation correctness
4. Django permission and authentication gaps (missing permission_classes)
5. Business logic bugs (diet engine, routine assignment, subscription enforcement)
6. Architectural misuse (signals, middleware, caching, Celery tasks)
7. Dead code and unused features (installed but non-functional)
8. API design issues (missing validation, inconsistent error handling)
9. Performance risks (N+1 queries, unindexed lookups, cache poisoning)
10. Production deployment risks (env vars, secrets, Dockerfile, fly.toml)

OUTPUT FORMAT per finding:
- Severity: CRITICAL / HIGH / MEDIUM / LOW / INFO
- Location: exact file + line range
- Issue: precise description (no vague language)
- Impact: what breaks or what attacker can do
- Fix: concrete code-level recommendation

Prioritize CRITICAL and HIGH findings first. Be exhaustive — this is a final 
pre-production scan. Missing a vulnerability is worse than a false positive.
```

---

## SECTION B: Audit Query (paste in first message with the bundle)

```
I've attached the complete Training Platform codebase bundle (fable_audit_bundle.txt).

Read SYSTEM_OVERVIEW.md first to understand the full architecture, then conduct 
the complete pre-production audit.

Audit sequence — work in this order:

PHASE 1 — SECURITY & AUTHENTICATION
Scan users/views.py, users/models.py, users/utils.py, and training_platform/middleware.py.
Find: auth bypass, OTP weaknesses, JWT misconfiguration, missing permission_classes,
privilege escalation between roles (client/trainer/agent/admin), IDOR vulnerabilities,
rate limiting gaps, missing authentication on any endpoint.

PHASE 2 — FINANCIAL SYSTEMS
Scan wallet/models.py, wallet/views.py, wallet/security.py.
Find: race conditions in fund transfers, audit log bypass, idempotency gaps,
agent API key weaknesses, any path that allows unauthorized balance modification,
escrow logic errors, missing HMAC or timestamp validation.

PHASE 3 — DIET AI PIPELINE
Scan diet/views.py (54 endpoints), diet/engine/, diet/ai/, diet/trainer_services.py.
Find: broken macro validation, permission gaps between trainer/client diets,
AI prompt injection risks, missing input bounds, logic errors in staged fill or 
macro balancing that could produce medically dangerous meal plans.

PHASE 4 — SUBSCRIPTION & PAYMENTS
Scan subscription/views.py, subscription/gateways/, subscription/utils.py.
Find: payment bypass vulnerabilities, webhook signature skipping, subscription 
enforcement gaps (can a user access premium features without active subscription?),
gateway-specific issues in Baraka/Bemo/Syriatel integrations.

PHASE 5 — ROUTINE & EXERCISE SYSTEM
Scan routine/views.py, routine/models.py, routine/permissions.py, routine/serializers.py.
Find: trainer/client boundary violations, session logging manipulation,
permission class consistency, serializer data leakage.

PHASE 6 — SUPPORTING SYSTEMS
Scan social/, ai_assistant/, achievements/, notifications/.
Find: WebSocket authentication gaps (consumers.py), AI tool-calling injection,
cost limit bypass in ai_assistant, notification system data leakage, 
achievement engine manipulation.

PHASE 7 — INFRASTRUCTURE & DEPLOYMENT
Scan training_platform/settings_*.py, middleware.py, Dockerfile, fly.toml.
Find: production config risks, secret exposure paths, middleware ordering issues,
Redis DB isolation violations, cachalot bypass table completeness,
any env var that could be omitted silently.

PHASE 8 — DEAD CODE & TECH DEBT
Identify: installed apps with no active routes, Celery tasks not scheduled,
signals with no listeners, models with no usage, features listed in requirements.txt
but not wired into the app, duplicate implementations across files.

---

After all 8 phases, deliver:

1. CRITICAL FINDINGS LIST (must fix before production)
2. HIGH FINDINGS LIST (should fix before production)  
3. DEAD CODE / UNUSED FEATURES (safe to remove for cleaner codebase)
4. TOP 5 ARCHITECTURAL UPGRADE RECOMMENDATIONS
5. PRODUCTION READINESS VERDICT: Ready / Not Ready + blockers list
```

---

## SECTION C: Follow-Up Queries (for deeper dives after the initial report)

Once Fable returns the initial audit, use these targeted follow-ups within the 
same Project conversation (token cost drops to ~$0.33/turn via caching):

**Deep dive on a specific finding:**
```
Expand on finding #[N]. Show me the exact vulnerable code path from the 
entry point (URL → view → model) and provide a complete, production-ready fix.
```

**Verify a specific flow end-to-end:**
```
Trace the complete trainer-assigns-diet-to-client flow from API entry to database 
write. Identify every permission check, validation step, and any missing guard.
```

**Unused features audit:**
```
List every model field, view, Celery task, and installed package that appears 
to be defined but has no callers, no routes, or no signal connections.
```

**Upgrade recommendations with effort:**
```
For each of your top 5 architectural recommendations, provide:
- Specific files to modify
- Estimated lines of change
- Risk of the change (low/medium/high)
- Priority order for implementation
```

---

## SECTION D: Token Budget Summary

| Turn | Tokens | Fable 5 API Cost | Pro Plan |
|---|---|---|---|
| First turn (bundle upload) | ~325K input + ~8K output | ~$3.66 | Counts against daily limit |
| Follow-up turns (cached) | ~32K input + ~4K output | ~$0.37 | Much cheaper |
| Full 8-phase + 4 follow-ups | ~450K total | ~$5.50 | Pro plan should cover |

**Pro plan usage tip:** If you hit the usage limit mid-audit, wait 1-2 hours and continue 
in the same Project — the file cache persists and the follow-up cost is minimal.

---

## SECTION E: What to Tell Fable in One Sentence (if you want it simpler)

If you want to skip the structured query, use this single prompt after uploading the bundle:

```
Read SYSTEM_OVERVIEW.md first. Then perform a complete pre-production audit of this 
Django 5.1 backend: find security vulnerabilities, financial logic flaws, permission 
gaps, broken business logic, dead code, and give me a production readiness verdict. 
Prioritize findings by severity. Be exhaustive — this is a final scan before production.
```
