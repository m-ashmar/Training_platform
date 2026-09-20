# Architecture

How the six integration rules in `FRONTEND_HANDOFF.md` §2 are enforced in code.

| Rule | Where | How |
|---|---|---|
| One API layer | `core/network/api_client.dart` | The only `Dio`. Base URL, bearer, refresh-on-401, `Accept-Language`, error decoding. Repositories call `getObject/postObject/…`; screens never see HTTP. |
| Branch on `code` | `core/network/api_error.dart`, `core/l10n/error_messages.dart` | `ApiCode` enum with the contract's codes; unknown codes fall to the HTTP-status default. One map from code to local copy. |
| One model per resource | `core/auth/app_user.dart` (pattern) | `fromJson` in one place, unknown fields ignored, `toJson` only for the local cache. |
| Feature flags | `core/config/feature_flags.dart` | One flag per moving area, off by default, set with `--dart-define`. |
| Media URLs expire | `AppUser.profilePictureUrl` doc | Only the parent record is cached; never the URL alone. |
| `Accept-Language` | `LanguageInterceptor` | Read at send time from `LocaleController`; a switch applies to the next request. |

## Auth state machine

```
AuthUnknown ──restore()──▶ AuthSignedOut ──login()/register()──▶ AuthUnverified(email)
     │                          ▲    ▲                                 │
     │ tokens + cached user     │    │ leaveVerification()             │ verifyOtp()
     ▼                          │    │                                 ▼
AuthSignedIn ◀──────────────────┘    └───────────── AuthSuspended ◀── account_disabled
     │ signOut() / SessionEvents.expired
     ▼
AuthSignedOut(reason)
```

- Login `400 {requires_verification: true}` → `AuthUnverified`, not a password error.
- `verify-otp` `403 account_disabled` → `AuthSuspended`; any other rejection is one
  generic message.
- `resend-otp` is always 200; a 429's `Retry-After` drives the cooldown.
- Refresh rotation: the interceptor stores the new refresh token on every refresh.
  Concurrent 401s queue behind one refresh (`QueuedInterceptor`). A rejected refresh
  clears tokens and emits `SessionEvents.expired`; the controller signs out and the
  welcome screen says why.
- Cold start renders from the cached user immediately and refreshes the profile
  behind it, so an offline start is still signed in.

## Routing

`GoRouter` with a `refreshListenable` bound to the auth state. Screens change state;
the redirect moves the user. No screen navigates "to login".

## Adding a feature

```
features/<area>/
  data/        <area>_repository.dart   (wire calls + DTO → model, nothing else)
  domain/      models
  presentation/ screens, widgets, controllers (Riverpod Notifier / AsyncNotifier)
```

- Repositories throw `ApiError`; controllers expose `AsyncValue`; screens switch on
  `ApiError.code`.
- Lists: page-number by default (`?page_size` ≤ 100); feeds and comments are cursor
  paginated, follow `next`, never build offsets, never re-sort client-side.
- Every string goes through `context.l10n`; server `detail` is a fallback only.
- Layout uses directional insets and alignments; numbers, phones and codes sit in `Ltr`.
