# Training Platform — mobile client

Flutter app for the Training Platform API. Built against
[`FRONTEND_HANDOFF.md`](../FRONTEND_HANDOFF.md) (2026-09-18); nothing older is
a source of truth for this client.

## Run

```sh
cd mobile
flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000
```

| Target | `API_BASE_URL` |
|---|---|
| iOS simulator | `http://127.0.0.1:8000` |
| Android emulator | `http://10.0.2.2:8000` |
| Physical device | your machine's LAN IP, backend started with `runserver 0.0.0.0:8000` |

Feature flags (`--dart-define=FF_PAYMENTS=true` etc.) are declared in
`lib/core/config/feature_flags.dart`; every area the handoff lists as still moving is
off by default.

## Verify

```sh
flutter analyze
flutter test
```

## Layout

```
lib/
  app.dart                  MaterialApp.router, theme + locale wiring
  main.dart                 bootstrap → runApp
  core/
    bootstrap.dart          prefs, language, auth restore before first frame
    config/                 Env (dart-define), FeatureFlags
    design/
      tokens/               palette, spacing, radii, motion, typography
      theme/                AppColors extension, AppTheme, theme mode
      components/           PrimaryButton, AppTextField, OtpCodeField, Ltr, Haptics
    network/                ApiClient (the only HTTP client), ApiError, interceptors
    auth/                   TokenStore, AppUser, AuthRepository, AuthController
    l10n/                   ARB files, locale controller, error → message map
    router/                 GoRouter, auth-driven redirects
  features/
    auth/presentation/      welcome, login, register, OTP, suspended
    shell/                  tab bar with train/eat temperature shift
    today/ train/ eat/ community/ me/
test/
  core/network/             ApiError decoding, refresh-on-401 (single flight, rotation)
  app_rtl_test.dart         Arabic first frame, language flip, signed-out routing
docs/
  DESIGN_FOUNDATION.md      the design language and its rules
  ARCHITECTURE.md           how the handoff's integration rules map to code
```

Fonts: IBM Plex Sans Arabic (SIL OFL 1.1, `assets/fonts/OFL.txt`).
