# API Contract Sync — Django ↔ Flutter

## Base URL
- Production: `https://training-platform-api.fly.dev`
- Local dev: `http://10.0.2.2:8000` (Android emulator) / `http://127.0.0.1:8000` (iOS simulator)

## API Prefix Convention
- All authenticated API routes: `/api/<app>/`
- Auth routes: `/api/auth/`
- No frontend-facing routes exist (pure REST API)

---

## Auth Endpoints (`/api/auth/`)

| Method | Path | Auth Required | Description |
|--------|------|:---:|---|
| POST | `/api/auth/register/` | ❌ | Register user → sends OTP |
| POST | `/api/auth/verify-otp/` | ❌ | Verify OTP → returns JWT |
| POST | `/api/auth/resend-otp/` | ❌ | Resend OTP (3/hr limit) |
| POST | `/api/auth/login/` | ❌ | Email+password login |
| POST | `/api/auth/token/` | ❌ | JWT obtain pair |
| POST | `/api/auth/token/refresh/` | ❌ | Refresh access token |
| POST | `/api/auth/token/logout/` | ✅ | Blacklist refresh token |
| GET/POST | `/api/auth/user/update/` | ✅ | Get or update user details |
| GET | `/api/auth/user/details/` | ✅ | Get user details |
| POST | `/api/auth/user/profile-picture/` | ✅ | Upload profile picture |
| POST | `/api/auth/forgot-password/` | ❌ | Request password reset OTP |
| POST | `/api/auth/forgot-password/verify/` | ❌ | Verify reset OTP |
| POST | `/api/auth/forgot-password/confirm/` | ❌ | Confirm new password |
| GET | `/api/auth/trainers/public/` | ❌ | List available trainers (paginated) |
| GET | `/api/auth/trainers/stats/` | ❌ | Trainer/client count stats |
| GET/POST | `/api/auth/trainer/profile/` | ✅ | Trainer profile |
| GET | `/api/auth/trainer/clients/` | ✅ | Trainer's client list |
| POST | `/api/auth/trainer/assign-client/` | ✅ | Trainer assigns client |
| POST | `/api/auth/trainer/unassign-client/` | ✅ | Trainer unassigns client |
| GET | `/api/auth/trainer/pending-requests/` | ✅ | Trainer sees pending requests |
| POST | `/api/auth/trainer/respond-to-request/` | ✅ | Trainer approves/rejects |
| GET/POST | `/api/auth/client/profile/` | ✅ | Client profile |
| GET | `/api/auth/client/available-trainers/` | ✅ | Client sees trainers |
| POST | `/api/auth/client/request-trainer/` | ✅ | Client requests trainer |
| GET | `/api/auth/client/request-status/` | ✅ | Client request status |
| POST | `/api/auth/client/unassign-trainer/` | ✅ | Client unassigns trainer |
| POST | `/api/auth/client/cancel-trainer-request/` | ✅ | Client cancels request |
| GET | `/api/auth/trainer/client-profile/<id>/` | ✅ | Trainer views approved client |
| POST | `/api/auth/device-token/` | ✅ | Register FCM token |

---

## Core Feature Endpoints

| Prefix | Namespace | Notes |
|--------|-----------|-------|
| `/api/routine/` | `routine` | Workout plans, exercises, templates |
| `/api/diet/` | `diet` | Meal plans, nutrition tracking, Edamam |
| `/api/subscription/` | `subscription` | Plans, subscriptions |
| `/api/wallet/` | `wallet` | Wallet, transactions, escrow |
| `/api/ai/` | `ai_assistant` | OpenAI GPT assistant |
| `/api/achievements/` | (no namespace) | Badges, milestones |
| (root) | — | analytics, social features |

---

## Shared Request/Response Contracts

### JWT Auth Header (all authenticated requests)
```
Authorization: Bearer <access_token>
Accept-Language: en   (or 'ar')
Content-Type: application/json
```

### Registration Request
```json
POST /api/auth/register/
{
  "username": "john_doe",
  "email": "john@example.com",
  "password1": "SecurePass123!",
  "password2": "SecurePass123!",
  "phone_number": "+1234567890",
  "user_type": "client"   // "client" | "trainer" | "agent"
}
```
**Response 201:**
```json
{
  "user": {"id": 1, "username": "...", "email": "...", "user_type": "client", ...},
  "message": "Registration successful. Please check your email for OTP verification code.",
  "requires_verification": true
}
```

### OTP Verification Request
```json
POST /api/auth/verify-otp/
{"email": "john@example.com", "otp_code": "123456"}
```
**Response 200:**
```json
{
  "message": "Email verified successfully.",
  "user": {"id": 1, "is_active": true, "onboarding_completed": false, ...},
  "access": "<jwt_access_token>",
  "refresh": "<jwt_refresh_token>"
}
```

### Login Request
```json
POST /api/auth/login/
{"email": "john@example.com", "password": "SecurePass123!"}
```
**Response 200:**
```json
{
  "access": "...", "refresh": "...",
  "user": {"id": 1, "user_type": "client", "onboarding_completed": true, ...}
}
```

### Token Refresh
```json
POST /api/auth/token/refresh/
{"refresh": "<refresh_token>"}
```
**Response 200:** `{"access": "<new_access_token>"}`

### Logout
```json
POST /api/auth/token/logout/
{"refresh_token": "<refresh_token>"}
```

---

## User Object Shape (in responses)

```json
{
  "id": 1,
  "username": "john_doe",
  "email": "john@example.com",
  "first_name": "John",
  "last_name": "Doe",
  "user_type": "client",        // "client" | "trainer" | "agent" | "admin"
  "profile_picture": "https://.../media/...",   // absolute URL or null
  "is_active": true,
  "onboarding_completed": false  // alias for is_onboarding_completed
}
```

**Trainer-specific fields:**
```json
{
  "trainer_bio": "...",
  "trainer_specializations": [...],
  "trainer_certifications": [...],
  "trainer_experience_years": 5,
  "trainer_hourly_rate": 50.0,
  "trainer_is_verified": true,
  "trainer_is_available": true,
  "client_count": 12
}
```

**Client-specific fields:**
```json
{
  "height": 175,
  "weight": 70,
  "age": 28,
  "gender": "male",
  "activity_level": "moderate",
  "specific_injury": null,
  "assigned_trainer": 5,       // trainer ID or null
  "assigned_trainer_name": "Jane Smith",
  "client_goals": [...],
  "client_preferences": [...]
}
```

---

## Pagination Contract

All list endpoints support pagination:
```
GET /api/auth/trainers/public/?page=2&search=ali
```
**Response shape:**
```json
{
  "count": 47,
  "next": "http://.../api/auth/trainers/public/?page=3",
  "previous": "http://.../api/auth/trainers/public/?page=1",
  "results": {
    "available_trainers": [...],
    "trainer_count": 47
  }
}
```
Some endpoints nest data inside `results`, others put it at top level — check each endpoint.

---

## Error Contract

**Validation errors (DRF standard):**
```json
{"field_name": ["Error message."]}
{"non_field_errors": ["..."]}
```

**Custom errors (business logic):**
```json
{"error": "Too many requests. Please try again later.", "retry_after": 3600}
{"error": "client_id is required"}
```

**Unverified user on login:**
```json
{
  "non_field_errors": ["Please verify your email address before logging in."],
  "requires_verification": true,
  "email": "john@example.com"
}
```

**Rate limited (429):**
```json
{"error": "Too many OTP requests. Please wait 1 hour before requesting again."}
```

---

## File Upload (Profile Picture)
```
POST /api/auth/user/profile-picture/
Content-Type: multipart/form-data

profile_picture: <file>
```
- Max size: 2MB
- Allowed types: JPEG, PNG, WebP

---

## Seam Points (Full-Stack Bug Hotspots)

1. **`profile_picture` URL** — Django returns **absolute URL** (with domain). Flutter must not prepend base URL again.
2. **`onboarding_completed`** field — it's aliased from `is_onboarding_completed` in the model. Field name in API is `onboarding_completed`.
3. **`user_type` routing** — register as `client` or `trainer`. Flutter must redirect to correct home screen based on this.
4. **OTP flow gate** — after register, NO tokens are returned. Tokens only come after `verify-otp/`. Flutter must handle `requires_verification: true` state.
5. **Language headers** — all error messages are translated. Send `Accept-Language: ar` to get Arabic errors. Missing header defaults to English.
6. **CORS** — `CORS_ALLOW_CREDENTIALS = True`. Flutter must not set `withCredentials` on native HTTP (only relevant for web).
7. **Trainer-Client request system** — both trainer and client can initiate; status is `pending` → `approved`/`rejected`. Refresh client state after status changes.
