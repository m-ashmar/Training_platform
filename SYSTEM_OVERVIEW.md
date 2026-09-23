# System Overview — Training Platform

> Last updated: July 2026  
> Source: verified against live codebase, not generated from documentation.

---

## 1. What This System Is

A **production-grade Django REST backend** serving a Flutter mobile application via REST APIs and WebSocket connections.

- **Client**: Flutter mobile app (iOS + Android)
- **Protocol**: REST (primary) + WebSocket (real-time features)
- **User roles**: `client`, `trainer`, `agent`, `admin`
- **Languages**: English + Arabic (full i18n via `django-modeltranslation`)
- **Scale**: Medium production — multi-tenant trainer/client model with financial transactions

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| **Language** | Python 3.12 |
| **Framework** | Django 5.1.3 |
| **API** | Django REST Framework 3.15.2 |
| **ASGI Server** | Daphne 4.x (WebSocket support) |
| **WSGI Server** | Gunicorn 24.x (fallback / HTTP-only) |
| **Database (prod)** | PostgreSQL (via `psycopg2-binary`) |
| **Database (dev)** | SQLite (`db.sqlite3`) |
| **ORM** | Django ORM |
| **Cache** | Redis + `django-redis` (5 segmented logical DBs) |
| **ORM-level cache** | `django-cachalot` (transparent query caching) |
| **Background Jobs** | Celery 5.5 + Redis broker |
| **WebSockets** | Django Channels 4.x + `channels-redis` |
| **Auth** | JWT (RS256 asymmetric) via `djangorestframework-simplejwt` |
| **Auth extras** | `django-allauth`, `dj-rest-auth` (custom OTP gating) |
| **Push Notifications** | Firebase Cloud Messaging (`firebase-admin`) |
| **File Validation** | `python-magic` (MIME-type server-side enforcement) |
| **Image Processing** | Pillow 11.x |
| **AI / LLM** | OpenAI (`gpt-4o-mini` default) + HuggingFace API |
| **Nutrition API** | Edamam (cached on Redis DB4) |
| **Payments** | Stripe + custom gateways (Baraka, Bemo, Syriatel) |
| **API Docs** | drf-yasg (Swagger/Redoc — admin-only in production) |
| **Monitoring** | Sentry SDK + structlog |
| **Static files** | WhiteNoise |
| **Containerization** | Docker (Python 3.12-slim) |
| **Deployment** | Fly.io (`training-platform-api`, region: `ams`) |

---

## 3. Settings Architecture

Settings are **split across 4 files** — no monolithic `settings.py`:

| File | Purpose |
|---|---|
| `settings_secrets.py` | Zero-trust env parsers: `require_env()`, `get_secret()`, AWS Secrets Manager loader |
| `settings_base.py` | Shared config — all secrets loaded via `require_env()` / `get_secret()`, no hardcoding |
| `settings_local.py` | Dev: `DEBUG=True`, LocMemCache, console email, opt-in `WALLET_DEV_MODE` |
| `settings_production.py` | Production: `DEBUG=False` hardcoded, full Redis segmentation, HSTS/SSL, `enforce_production_safety()` kill-switch |

`settings.py` at root acts as environment router only.

---

## 4. Installed Django Apps (12 project apps)

| App | Responsibility |
|---|---|
| `users` | Auth, registration, OTP, trainer–client relationships, device tokens |
| `routine` | Workout routines, exercise templates, set logging, session tracking |
| `diet` | AI-powered meal planning, nutrition tracking, food database, trainer diet management |
| `subscription` | Plans, payments, multi-gateway (Stripe, Baraka, Bemo, Syriatel) |
| `wallet` | Internal wallet, escrow, agent API keys, tamper-evident audit logs, atomic transactions |
| `achievements` | Achievement engine, registry, signal-driven award system |
| `social` | Social feed, posts, likes, follows, comments, real-time via WebSocket |
| `analytics` | User progress analytics, workout session analysis |
| `notifications` | Multi-channel notification dispatch (FCM, in-app), event-driven listeners |
| `challenges` | Fitness challenges system |
| `ai_assistant` | Conversational AI coach — tool-calling, context compilation, cost tracking |
| `admin_dashboard` | Custom admin UI (replaces default Django admin at `/dj-admin/`) |

---

## 5. URL Structure & Route Count

| Prefix | App | Routes |
|---|---|---|
| `/api/auth/` | `users` | 29 |
| `/api/diet/` | `diet` | 54 |
| `/api/routine/` | `routine` | 10 |
| `/api/wallet/` | `wallet` | 12 |
| `/api/ai/` | `ai_assistant` | 6 |
| `/api/subscription/` | `subscription` | 8 |
| `/api/` | `achievements` | 3 |
| `/` (analytics) | `analytics` | 2 |
| `/` (social) | `social` | 2 |
| `/dj-admin/` | `admin_dashboard` | 2 |
| **Total** | | **~128 routes** |

API versioning: `/api/` prefix (no explicit `v1` in URL — version via `APIVersionMiddleware`).  
Swagger/Redoc: available only in DEBUG mode at `/swagger/` and `/redoc/`.

---

## 6. Authentication & Security Model

### JWT (RS256 — asymmetric)
- **Algorithm**: RS256 — private key signs, public key verifies. Keys loaded from secrets manager.
- **Access token**: 60 minutes
- **Refresh token**: 7 days, rotated + blacklisted on use
- **User model**: `users.CustomUser` (extends `AbstractUser`)

### User Roles
```
client  → end-user (athlete)
trainer → professional trainer, manages client routines and diet plans
agent   → financial agent with wallet and API key access
admin   → platform superuser
```

### OTP Flow
- Custom `CustomAccountAdapter` gates the allauth signup/login flow
- OTP generated via `secrets` module (cryptographic)
- OTP stored as hash, validated via constant-time comparison
- Rate-limited with attempt lockout (5 failures → lock)

### Middleware Stack (in order)
```
1. SecurityMiddleware          ← must be first (Django requirement)
2. SecurityHeadersMiddleware   ← custom HSTS, CSP headers
3. SessionMiddleware
4. CorsMiddleware
5. CommonMiddleware
6. CsrfViewMiddleware
7. AuthenticationMiddleware
8. MessageMiddleware
9. XFrameOptionsMiddleware
10. AccountMiddleware          ← allauth
11. LanguageResolutionMiddleware ← Arabic/English per request
12. RateLimitMiddleware
13. RequestLoggingMiddleware
14. DatabaseQueryCountMiddleware
15. CacheMiddleware
16. APIVersionMiddleware
17. ErrorHandlingMiddleware
```

---

## 7. Redis Architecture (5 Logical DBs)

| DB | Purpose | Cache alias |
|---|---|---|
| DB0 | Sessions | `default` (also ORM cachalot) |
| DB1 | Rate limiting (`django-ratelimit`) | `ratelimit` |
| DB2 | Public cache (anonymous responses) | `public` |
| DB3 | Private cache (user-scoped data) | `private` |
| DB4 | Edamam nutrition API cache (24h TTL) | `edamam` |
| DB5 | Django Channels layer (WebSocket messages) | `CHANNEL_LAYERS` |

**cachalot bypass list** (race-condition safety): `wallet_*`, `auth_user`, `users_customuser`, `django_session`, all permission tables.

---

## 8. Background Jobs (Celery)

| App | Task |
|---|---|
| `ai_assistant` | `close_idle_sessions` (10min), `compute_all_user_insights` (24h), `check_daily_cost` (1h) |
| `diet` | Meal plan computation, nutrition recalculation (6 tasks) |
| `social` | Feed cache invalidation, post processing (4 tasks) |
| `notifications` | FCM dispatch, batch notifications (2 tasks) |
| `routine` | Session summary computation (1 task) |

Broker: Redis. `CELERY_TASK_ALWAYS_EAGER=False` in production (real async workers).

---

## 9. WebSocket Consumers

| Consumer | App | Purpose |
|---|---|---|
| `AIAssistantConsumer` | `ai_assistant` | Real-time AI chat with tool-calling |
| `SocialConsumer` | `social` | Live social feed updates, notifications |

Channel layer: Redis DB5.

---

## 10. Diet AI System

The most complex subsystem. Fully layered:

```
diet/
├── ai/               ← LLM integration (OpenAI prompt builder, response handler, generator)
├── engine/           ← Rule-based planner, meal factory, rebalancer, validator,
│                        macro balancer, calorie trimmer, fat capper, snack enforcer,
│                        staged fill algorithm
├── services/         ← payment_gateways.py (diet-specific services)
├── experimental/     ← Staged fill v2 experiments
├── views.py          ← 54 API endpoints (~92K bytes)
├── tasks.py          ← 6 Celery tasks
├── trainer_services.py ← Trainer-specific diet operations
└── meal_processor.py ← Core meal processing pipeline
```

Feature flags in `settings_base.py`:
- `DIET_SMART_MACRO_PLANNER = True`
- `DIET_DYNAMIC_MEAL_ALLOCATION = True`
- `DIET_STAGED_MEAL_FILL = True`

---

## 11. AI Assistant System

Conversational coach with tool-calling architecture:

```
ai_assistant/
├── tools/       ← diet_tools, routine_tools, training_tools, progress_tools, user_tools
├── analyzers/   ← behavior_profiler, diet_analyzer, training_analyzer
├── services/    ← chat_service, context_compiler, memory_service,
│                   cost_tracker, data_collector, security
├── consumers.py ← WebSocket handler
└── tool_registry.py ← Tool registration and dispatch
```

Config: max 50 messages/day, 30-min session timeout, $50/day cost alert, `gpt-4o-mini` default model.

---

## 12. Wallet & Financial System

Financial-grade controls:

- **Atomic transfers**: `transfer_funds()` wrapped in `transaction.atomic()` with `select_for_update`
- **Audit log**: `WalletAuditLog` — append-only, tamper-evident (hash chain per entry)
- **Agent API keys**: `AgentAPIKey` — raw key never stored, only HMAC/SHA-256 hash
- **Idempotency**: `IdempotencyKey` model prevents duplicate transactions
- **WALLET_DEV_MODE**: hardcoded `False` in production settings — no env override possible
- **Escrow**: Platform escrow account (`PLATFORM_ESCROW_EMAIL`) for trainer–client payment holds
- **Wallet types**: `client`, `trainer`, `agent`

---

## 13. Notification System

Event-driven, multi-channel:

```
notifications/
├── channels/    ← fcm.py (Firebase Cloud Messaging)
├── domain/      ← events.py, trainer_client_events.py
├── listeners/   ← trainer_client_listeners.py, social_listeners.py
├── dispatcher.py
└── template_resolver.py
```

Push notification: Firebase Admin SDK. Device tokens stored per-user, per-platform (`android`/`ios`).

---

## 14. File Security

`training_platform/file_security.py` (419 lines):
- Server-side MIME type validation via `python-magic`
- Extension whitelist enforcement
- Max file size per upload type
- Prevents polyglot/spoofed file uploads

---

## 15. Subscription & Payments

```
subscription/gateways/
├── baraka.py     ← Baraka payment gateway
├── bemo.py       ← Bemo gateway
└── syriatel.py   ← Syriatel Cash

subscription/services/
└── payment_gateways.py
```

Also integrates: Stripe (`stripe==7.8.0`).  
Subscription plans with trial periods, renewal logic, and status enforcement at API permission layer.

---

## 16. Internationalization

- Languages: **English + Arabic** (RTL-aware)
- Engine: `django-modeltranslation` (model-level field translation)
- Middleware: `LanguageResolutionMiddleware` (per-request language from `Accept-Language` or user preference)
- Locale path: `/locale/`
- Error handlers: `handler404`, `handler500` are language-aware (no default English leakage)

---

## 17. Production Security Posture

| Control | Implementation |
|---|---|
| DEBUG | `False` — hardcoded in `settings_production.py` |
| Secrets | Zero hardcoded secrets — all via `require_env()` / `get_secret()` / AWS Secrets Manager |
| Secret key rotation | `SECRET_KEY_FALLBACKS` supports hitless rotation |
| HSTS | `SECURE_HSTS_SECONDS = 31536000` (1 year), preload + subdomains |
| SSL | `SECURE_SSL_REDIRECT = True`, `SECURE_PROXY_SSL_HEADER` |
| Cookies | `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`, `SameSite=Lax` |
| CORS | Strict allowlist via `CORS_ALLOWED_ORIGINS` env var |
| Rate limiting | 3-layer: CDN/WAF → NGINX → `django-ratelimit` (Redis DB1) |
| JWT signing | RS256 asymmetric — private key signs, public key verifies |
| OTP | `secrets` module, hash-stored, constant-time compare, 5-attempt lockout |
| Production kill-switch | `enforce_production_safety()` crashes process on misconfiguration |
| Wallet dev mode | `WALLET_DEV_MODE = False` hardcoded — no env override path |
| Audit logs | `WalletAuditLog` — append-only, hash-chained, tamper-evident |

---

## 18. Deployment

| Item | Value |
|---|---|
| Platform | Fly.io |
| App name | `training-platform-api` |
| Region | `ams` (Amsterdam) |
| Container | Docker — `python:3.12-slim` |
| Server | Daphne (ASGI — required for WebSockets) |
| Memory | 512MB |
| CPU | 1 shared vCPU |
| Scale-to-zero | Enabled (`min_machines_running = 0`) |
| Health check | `GET /api/auth/health/` every 30s |
| Static files | WhiteNoise (production), `collectstatic` at build time |
| Container user | Non-root (`appuser`) |

---

## 19. Architecture Pattern

```
Modular Django Monolith
│
├── HTTP Layer:    DRF ViewSets / APIViews (per-app urls.py)
├── Business Logic: views.py + services.py + app-specific service modules
├── Data Layer:    Django ORM + cachalot (transparent caching)
├── Task Layer:    Celery (async/scheduled jobs)
├── Realtime Layer: Django Channels + Redis (WebSocket)
└── External APIs: OpenAI, Edamam, Firebase, Stripe, Baraka, Bemo, Syriatel
```

**Core size**: ~270 source files, ~46,000 lines of Python (excluding tests and migrations).