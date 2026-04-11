# Security Hardening Walkthrough

## Summary

Transformed the Training Platform from a development-mode configuration to an enterprise-grade, zero-trust security posture across **12 remediation areas**, modifying **20+ files** and creating **10 new files**.

---

## Changes Made

### 1. Settings Split & Secrets Management

| File | Action | Purpose |
|------|--------|---------|
| [settings_secrets.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_secrets.py) | NEW | Zero-trust env parsers: [require_env()](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_secrets.py#18-27), [get_bool_env()](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_secrets.py#34-50), AWS Secrets Manager |
| [settings_base.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_base.py) | NEW | Shared config — zero hardcoded secrets, all via [require_env()](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_secrets.py#18-27) |
| [settings_local.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_local.py) | NEW | Local dev: DEBUG=True, LocMemCache, console email, explicit WALLET_DEV_MODE opt-in |
| [settings_production.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_production.py) | NEW | Production: DEBUG=False hardcoded, Redis segmented (DB0-4), full HSTS/SSL, [enforce_production_safety()](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_production.py#172-200) kill-switch |
| [settings.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings.py) | MODIFIED | Thin import shim for backward compatibility |
| [.env](file:///Users/mac/Desktop/Git/t2/Training_platform/.env) | MODIFIED | Stripped exposed credentials, placeholder values only |
| [.env.example](file:///Users/mac/Desktop/Git/t2/Training_platform/.env.example) | NEW | Documents all required/optional env vars |

**Key design decisions:**
- `SECRET_KEY` and `JWT_SIGNING_KEY` are separate (independent rotation)
- `SECRET_KEY_FALLBACKS` supports hitless key rotation
- `SecurityMiddleware` is FIRST in MIDDLEWARE (was second)
- Production settings crash on missing secrets (`ImproperlyConfigured`)

---

### 2. Wallet Security — Dev-Mode Elimination

| File | Changes |
|------|---------|
| [wallet/views.py](file:///Users/mac/Desktop/Git/t2/Training_platform/wallet/views.py) | Removed **7** `WALLET_DEV_MODE` bypasses. HMAC/IP/timestamp checks now unconditional. Deleted `AdminDevCreateAgentView`. |
| [wallet/urls.py](file:///Users/mac/Desktop/Git/t2/Training_platform/wallet/urls.py) | Removed `admin/dev/create-agent/` route |
| [middleware.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/middleware.py) | Rate limiting always enforced (DEBUG gets higher limits, not disabled) |

---

### 3. OTP Cryptographic Hardening

| File | Changes |
|------|---------|
| [users/utils.py](file:///Users/mac/Desktop/Git/t2/Training_platform/users/utils.py) | `secrets.randbelow` replaces `random.randint`, SHA-256 hashed storage, `secrets.compare_digest` for constant-time verification, 5-attempt lockout with 15min cooldown, anti-enumeration on `DoesNotExist` |

---

### 4. Auth System — dj-rest-auth Bypass Prevention

| File | Changes |
|------|---------|
| [users/adapters.py](file:///Users/mac/Desktop/Git/t2/Training_platform/users/adapters.py) | NEW — `CustomAccountAdapter.is_open_for_signup()` returns `False` |
| [settings_base.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_base.py) | `ACCOUNT_ADAPTER` set to custom adapter |
| [urls.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/urls.py) | Removed `dj_rest_auth.urls` include, removed duplicate namespace |

---

### 5. Tamper-Proof Audit Logging

| File | Changes |
|------|---------|
| [wallet/models.py](file:///Users/mac/Desktop/Git/t2/Training_platform/wallet/models.py) | Added `prev_hash` + `entry_hash` fields, [save()](file:///Users/mac/Desktop/Git/t2/Training_platform/users/models.py#390-410) computes SHA-256 hash chain |
| [wallet/migrations/0003_add_audit_hash_chain.py](file:///Users/mac/Desktop/Git/t2/Training_platform/wallet/migrations/0003_add_audit_hash_chain.py) | NEW — Migration for hash chain fields |

---

### 6. Firebase, Swagger, Entry Points

| File | Changes |
|------|---------|
| [social/firebase_service.py](file:///Users/mac/Desktop/Git/t2/Training_platform/social/firebase_service.py) | Fail-closed init: crashes if `FIREBASE_CREDENTIALS_PATH` missing |
| [urls.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/urls.py) | Swagger hidden in production, admin-only permission |
| [wsgi.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/wsgi.py) | Defaults to production settings, calls [enforce_production_safety()](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_production.py#172-200) |
| [asgi.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/asgi.py) | Defaults to production settings |
| [celery.py](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/celery.py) | Defaults to production settings |

---

### 7. CI/CD & Pre-commit

| File | Purpose |
|------|---------|
| [.github/workflows/security.yml](file:///Users/mac/Desktop/Git/t2/Training_platform/.github/workflows/security.yml) | Bandit + Safety + Semgrep + TruffleHog + hardcoded secret scan on every PR |
| [.pre-commit-config.yaml](file:///Users/mac/Desktop/Git/t2/Training_platform/.pre-commit-config.yaml) | detect-secrets + bandit + private key detection |

---

## Validation Results

| Check | Result |
|-------|--------|
| Hardcoded secrets in Python files | ✅ **Zero matches** |
| `WALLET_DEV_MODE` in business logic | ✅ **Zero references** (only in settings + tests) |
| `random.randint` in OTP | ✅ **Replaced** with `secrets.randbelow` |
| `dj_rest_auth` bypass URL | ✅ **Removed** |
| `SecurityMiddleware` position | ✅ **First** in MIDDLEWARE |
| `compare_digest` in OTP verification | ✅ **Implemented** |
| Swagger public access | ✅ **Admin-only** and **hidden in production** |
| Production runtime guards | ✅ [enforce_production_safety()](file:///Users/mac/Desktop/Git/t2/Training_platform/training_platform/settings_production.py#172-200) in wsgi.py |

---

## Post-Implementation Action Items for User

> [!IMPORTANT]
> 1. **Rotate ALL leaked credentials** (OpenAI, Gmail, HuggingFace, Firebase, Edamam) — they are in git history
> 2. **Fill in [.env](file:///Users/mac/Desktop/Git/t2/Training_platform/.env)** with your new credentials for local development
> 3. **Run migration**: `python manage.py migrate wallet` (applies audit hash chain)
> 4. **Install pre-commit**: `pip install pre-commit && pre-commit install`
> 5. **Test locally**: `DJANGO_SETTINGS_MODULE=training_platform.settings_local python manage.py runserver`
