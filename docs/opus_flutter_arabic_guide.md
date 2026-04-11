# Flutter Arabic Integration — Agent Execution Guide

> **For:** Claude Opus (or any AI agent executing this on the Flutter codebase)
> **Context:** The Django backend is fully wired for multilingual support. All APIs return translated content based on `Accept-Language` header. This guide tells you exactly what to build on the Flutter side.
> **Rule:** Follow phases in order. Do not skip. Verify each phase before moving to the next.

---

## Backend API Contract (READ FIRST)

You are integrating against a Django REST Framework backend. Here is exactly how language works:

### How the backend resolves language

```
Priority order:
1. Accept-Language HTTP header (e.g., "ar" or "en")
2. JWT user's preferred_language field (extracted from token)
3. Session user's preferred_language
4. Default: "en"
```

### User model language field

```
PATCH /api/auth/user/
Body: { "preferred_language": "ar" }   // choices: "en", "ar"
```

This field controls:
- Push notification language (resolved at delivery time from DB)
- Email language
- Cached API response language

### What changes per language in API responses

| Endpoint | Translated fields |
|----------|------------------|
| `GET /api/routine/exercises/` | `name`, `description` (django-modeltranslation) |
| `GET /api/routine/templates/` | `name`, `description` |
| `GET /api/diet/food-items/` | `name` |
| `GET /api/diet/food-categories/` | `name` |
| `GET /api/achievements/` | `name`, `description` |
| `GET /api/social/notifications/` | NOT pre-translated — contains `event_type` + `metadata` |
| `POST /api/ai/chat/` | AI responses follow `Accept-Language` |
| Any 400/422 error | `detail` field is translated |
| Any 404/500 error | `detail` field is translated |

### Push notifications

Notifications arrive **already translated** via FCM. The backend resolves the user's `preferred_language` from the database at the moment of delivery. **Do not translate push notification content client-side.**

### WebSocket (AI Chat)

```
wss://{host}/ws/ai/chat/
Headers: Authorization: Bearer {token}
```

The backend activates `LanguageContext.for_user()` per message. AI responses arrive in the user's preferred language. Just send `Accept-Language` header on connection if possible.

---

## Phase 1 — Project Setup

### 1.1 Add dependencies to `pubspec.yaml`

```yaml
dependencies:
  flutter_localizations:
    sdk: flutter
  intl: ^0.19.0

flutter:
  generate: true
```

### 1.2 Create `l10n.yaml` in project root

```yaml
arb-dir: lib/l10n
template-arb-file: app_en.arb
output-localization-file: app_localizations.dart
output-class: AppLocalizations
```

### 1.3 Create directory `lib/l10n/`

Create two files: `app_en.arb` and `app_ar.arb`.

**Instructions for populating ARB files:**
1. Search the entire codebase for every hardcoded English string in widgets
2. Extract each into the English ARB as a key-value pair
3. Translate each into the Arabic ARB
4. Use ICU message format for plurals and parameters

**ARB format:**
```json
{
  "@@locale": "en",
  "keyName": "English text",
  "keyNameWithParam": "Hello {name}",
  "@keyNameWithParam": {
    "placeholders": {
      "name": { "type": "String" }
    }
  }
}
```

**Common keys you will definitely need (translate all to Arabic in `app_ar.arb`):**

```json
{
  "@@locale": "en",
  "appTitle": "Training Platform",
  "login": "Login",
  "register": "Register",
  "email": "Email",
  "password": "Password",
  "forgotPassword": "Forgot Password?",
  "home": "Home",
  "profile": "Profile",
  "settings": "Settings",
  "language": "Language",
  "arabic": "Arabic",
  "english": "English",
  "save": "Save",
  "cancel": "Cancel",
  "delete": "Delete",
  "confirm": "Confirm",
  "loading": "Loading...",
  "error": "An error occurred",
  "retry": "Retry",
  "noData": "No data available",
  "notifications": "Notifications",
  "routines": "Routines",
  "dietPlans": "Diet Plans",
  "exercises": "Exercises",
  "achievements": "Achievements",
  "challenges": "Challenges",
  "aiAssistant": "AI Assistant",
  "trainer": "Trainer",
  "clients": "Clients",
  "approve": "Approve",
  "reject": "Reject",
  "logout": "Logout",
  "search": "Search",
  "submit": "Submit",
  "next": "Next",
  "back": "Back",
  "done": "Done",
  "welcome": "Welcome",
  "totalCalories": "Total Calories",
  "protein": "Protein",
  "carbs": "Carbs",
  "fat": "Fat",
  "sets": "Sets",
  "reps": "Reps",
  "weight": "Weight",
  "duration": "Duration",
  "startDate": "Start Date",
  "endDate": "End Date",
  "today": "Today",
  "yesterday": "Yesterday",
  "noNotifications": "No notifications yet",
  "changeLanguage": "Change Language",
  "darkMode": "Dark Mode"
}
```

**Arabic translations for the above:**

```json
{
  "@@locale": "ar",
  "appTitle": "منصة التدريب",
  "login": "تسجيل الدخول",
  "register": "إنشاء حساب",
  "email": "البريد الإلكتروني",
  "password": "كلمة المرور",
  "forgotPassword": "نسيت كلمة المرور؟",
  "home": "الرئيسية",
  "profile": "الملف الشخصي",
  "settings": "الإعدادات",
  "language": "اللغة",
  "arabic": "العربية",
  "english": "الإنجليزية",
  "save": "حفظ",
  "cancel": "إلغاء",
  "delete": "حذف",
  "confirm": "تأكيد",
  "loading": "جاري التحميل...",
  "error": "حدث خطأ",
  "retry": "إعادة المحاولة",
  "noData": "لا توجد بيانات",
  "notifications": "الإشعارات",
  "routines": "البرامج التدريبية",
  "dietPlans": "خطط التغذية",
  "exercises": "التمارين",
  "achievements": "الإنجازات",
  "challenges": "التحديات",
  "aiAssistant": "المساعد الذكي",
  "trainer": "المدرب",
  "clients": "العملاء",
  "approve": "قبول",
  "reject": "رفض",
  "logout": "تسجيل الخروج",
  "search": "بحث",
  "submit": "إرسال",
  "next": "التالي",
  "back": "رجوع",
  "done": "تم",
  "welcome": "مرحباً",
  "totalCalories": "إجمالي السعرات",
  "protein": "بروتين",
  "carbs": "كربوهيدرات",
  "fat": "دهون",
  "sets": "مجموعات",
  "reps": "تكرارات",
  "weight": "الوزن",
  "duration": "المدة",
  "startDate": "تاريخ البداية",
  "endDate": "تاريخ النهاية",
  "today": "اليوم",
  "yesterday": "أمس",
  "noNotifications": "لا توجد إشعارات",
  "changeLanguage": "تغيير اللغة",
  "darkMode": "الوضع الداكن"
}
```

### 1.4 Run code generation

```bash
flutter gen-l10n
```

Verify `lib/l10n/app_localizations.dart` is generated.

---

## Phase 2 — App Root Configuration

Find the `MaterialApp` widget (usually in `main.dart` or `app.dart`) and modify:

```dart
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_gen/gen_l10n/app_localizations.dart';

MaterialApp(
  // ADD these:
  locale: languageProvider.locale,  // from state management
  supportedLocales: const [
    Locale('en'),
    Locale('ar'),
  ],
  localizationsDelegates: const [
    AppLocalizations.delegate,
    GlobalMaterialLocalizations.delegate,
    GlobalWidgetsLocalizations.delegate,
    GlobalCupertinoLocalizations.delegate,
  ],
  // DO NOT add a manual Directionality builder — Flutter handles RTL
  // automatically when locale is set to 'ar'
)
```

---

## Phase 3 — Language State Management

Create or modify a language provider/controller. Must support:

1. **Load saved language** from `SharedPreferences` on app start
2. **Change language** → save locally + PATCH backend + update API headers
3. **Expose current locale** to the widget tree

```dart
class LanguageProvider extends ChangeNotifier {
  Locale _locale = const Locale('en');

  Locale get locale => _locale;
  bool get isArabic => _locale.languageCode == 'ar';
  bool get isRtl => isArabic;

  /// Call on app startup
  Future<void> init() async {
    final prefs = await SharedPreferences.getInstance();
    final saved = prefs.getString('preferred_language') ?? 'en';
    _locale = Locale(saved);
    _updateApiHeaders(saved);
    notifyListeners();
  }

  /// Call when user switches language
  Future<void> setLanguage(String code) async {
    if (code != 'en' && code != 'ar') return;
    _locale = Locale(code);

    // 1. Persist locally
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString('preferred_language', code);

    // 2. Update API client header
    _updateApiHeaders(code);

    // 3. Sync to backend (non-blocking)
    _syncToBackend(code);

    notifyListeners();
  }

  void _updateApiHeaders(String code) {
    // Find wherever your Dio/http client is configured
    // and set: headers['Accept-Language'] = code;
  }

  Future<void> _syncToBackend(String code) async {
    try {
      // PATCH /api/auth/user/ with { "preferred_language": code }
    } catch (_) {
      // Non-critical — backend falls back to Accept-Language header
    }
  }
}
```

**Wire into widget tree:**
```dart
ChangeNotifierProvider(create: (_) => LanguageProvider()..init())
```

---

## Phase 4 — API Client Header Injection

**This is the single most important step.** Every API call must include `Accept-Language`.

Find the HTTP client setup (Dio, http package, or custom).

**For Dio:**
```dart
dio.interceptors.add(InterceptorsWrapper(
  onRequest: (options, handler) {
    options.headers['Accept-Language'] = currentLanguageCode;
    return handler.next(options);
  },
));
```

**For http package:**
```dart
Map<String, String> get defaultHeaders => {
  'Authorization': 'Bearer $token',
  'Accept-Language': currentLanguageCode,  // ADD THIS
  'Content-Type': 'application/json',
};
```

**Verification:** After this step, change language to Arabic and make any API call. Check that:
- Exercise names come back in Arabic (if Arabic translations exist in backend)
- Error messages come back in Arabic

---

## Phase 5 — RTL Layout Audit

### What Flutter does automatically when locale is Arabic:
- `Row` children reverse
- `ListView` items render right-to-left
- `Drawer` opens from right
- `AppBar` back button moves to right
- Start/End padding flips

### What you MUST manually fix:

**Search entire codebase for these patterns and replace:**

| Find | Replace with |
|------|-------------|
| `EdgeInsets.only(left:` | `EdgeInsetsDirectional.only(start:` |
| `EdgeInsets.only(right:` | `EdgeInsetsDirectional.only(end:` |
| `Alignment.centerLeft` | `AlignmentDirectional.centerStart` |
| `Alignment.centerRight` | `AlignmentDirectional.centerEnd` |
| `Alignment.topLeft` | `AlignmentDirectional.topStart` |
| `Alignment.topRight` | `AlignmentDirectional.topEnd` |
| `Alignment.bottomLeft` | `AlignmentDirectional.bottomStart` |
| `Alignment.bottomRight` | `AlignmentDirectional.bottomEnd` |
| `CrossAxisAlignment.start` | Keep — this is already directional |
| `TextAlign.left` | `TextAlign.start` |
| `TextAlign.right` | `TextAlign.end` |

> **Exception:** `EdgeInsets.symmetric()` is fine as-is. Only fix asymmetric padding.

### Things that must stay LTR regardless of locale:
- Phone numbers
- OTP input
- Prices / calorie numbers (discuss with designer)
- Progress bars
- Charts
- Media playback controls

Wrap those in:
```dart
Directionality(
  textDirection: TextDirection.ltr,
  child: ...
)
```

---

## Phase 6 — String Replacement

### Process:
1. Search all `.dart` files for hardcoded English strings in widgets
2. For each string:
   a. Add it to `app_en.arb` with a descriptive key
   b. Add the Arabic translation to `app_ar.arb`
   c. Replace in code: `'Loading...'` → `AppLocalizations.of(context)!.loading`
3. Run `flutter gen-l10n` after adding new keys

### How to find hardcoded strings:
```bash
# In the Flutter project:
grep -rn "Text('" lib/ --include="*.dart" | grep -v "import\|//\|test"
grep -rn 'Text("' lib/ --include="*.dart" | grep -v "import\|//\|test"
grep -rn "hintText:" lib/ --include="*.dart"
grep -rn "labelText:" lib/ --include="*.dart"
grep -rn "title:" lib/ --include="*.dart" | grep -i "text\|string"
grep -rn "AppBar(" lib/ --include="*.dart"
grep -rn "SnackBar(" lib/ --include="*.dart"
grep -rn "AlertDialog(" lib/ --include="*.dart"
grep -rn "tooltip:" lib/ --include="*.dart"
```

### Common file locations to check:
- `lib/screens/` or `lib/pages/` — every screen
- `lib/widgets/` — reusable components
- `lib/components/` — UI components
- `lib/dialogs/` — alert/confirm dialogs
- `lib/utils/` — error message utilities

---

## Phase 7 — Arabic Font

### 7.1 Download Cairo font

Download from Google Fonts: Regular (400), SemiBold (600), Bold (700).
Place in `assets/fonts/`.

### 7.2 Register in pubspec.yaml

```yaml
flutter:
  fonts:
    - family: Cairo
      fonts:
        - asset: assets/fonts/Cairo-Regular.ttf
        - asset: assets/fonts/Cairo-SemiBold.ttf
          weight: 600
        - asset: assets/fonts/Cairo-Bold.ttf
          weight: 700
```

### 7.3 Apply conditionally in theme

```dart
final isArabic = locale.languageCode == 'ar';
ThemeData(
  fontFamily: isArabic ? 'Cairo' : null,  // null = system default
  textTheme: isArabic
    ? Theme.of(context).textTheme.apply(fontFamily: 'Cairo')
    : null,
)
```

Arabic text typically needs ~10% more line height:
```dart
TextStyle(
  height: isArabic ? 1.7 : 1.5,
)
```

---

## Phase 8 — Date & Number Formatting

### Use `intl` package, NOT hardcoded formats

```dart
import 'package:intl/intl.dart';

// Date
DateFormat.yMMMd(locale).format(dateTime);
// en → "Mar 4, 2026"
// ar → "٤ مارس ٢٠٢٦"

// Number
NumberFormat('#,##0', locale).format(1500);
// en → "1,500"
// ar → "١٬٥٠٠"

// Relative time
// Use timeago package with Arabic locale, or build custom
```

### Decision: Eastern vs Western Arabic numerals

Many Arabic fitness apps use Western numerals (0-9) for workout data.
If the team wants Western numerals everywhere:

```dart
// Force Western numerals
NumberFormat('#,##0', 'en').format(value); // Always use 'en' locale for numbers
```

---

## Phase 9 — Language Switcher Widget

Add to Settings screen:

```dart
ListTile(
  leading: Icon(Icons.language),
  title: Text(AppLocalizations.of(context)!.language),
  subtitle: Text(
    context.read<LanguageProvider>().isArabic
      ? AppLocalizations.of(context)!.arabic
      : AppLocalizations.of(context)!.english,
  ),
  trailing: Switch(
    value: context.watch<LanguageProvider>().isArabic,
    onChanged: (val) {
      context.read<LanguageProvider>().setLanguage(val ? 'ar' : 'en');
    },
  ),
)
```

**Important:** Language change must NOT require app restart. The `MaterialApp` rebuilds when `locale` changes via the provider.

---

## Phase 10 — In-App Notifications

The notification list endpoint returns raw `event_type` + `metadata`. Build display text client-side:

```dart
String notificationText(String eventType, Map<String, dynamic> meta, AppLocalizations l10n) {
  switch (eventType) {
    case 'post_liked':
      return l10n.userLikedYourPost(meta['actor_name'] ?? '');
    case 'comment_created':
      return l10n.userCommentedOnYourPost(meta['actor_name'] ?? '');
    case 'user_followed':
      return l10n.userFollowedYou(meta['actor_name'] ?? '');
    case 'achievement_awarded':
      return l10n.achievementUnlocked(meta['achievement_name'] ?? '');
    case 'trainer_assignment_request':
      return l10n.trainerRequestReceived(meta['trainer_name'] ?? '');
    case 'client_request_approved':
      return l10n.requestApproved;
    case 'client_request_rejected':
      return l10n.requestRejected;
    default:
      return l10n.newNotification;
  }
}
```

Add all these keys to both ARB files with Arabic translations.

---

## Verification Checklist

After completing all phases, verify each screen:

### Functional checks:
- [ ] App starts in saved language preference
- [ ] Language switch works without restart
- [ ] API responses come in selected language (check exercise names)
- [ ] Error messages from API are in selected language
- [ ] Push notifications arrive in selected language
- [ ] Language persists across app restart
- [ ] `PATCH /api/auth/user/` is called on language change

### RTL layout checks (switch to Arabic):
- [ ] Login/Register screens — fields and buttons align right
- [ ] Home/Dashboard — cards and stats flip correctly
- [ ] Navigation drawer opens from right
- [ ] Bottom nav bar — icons stay centered, labels are Arabic
- [ ] Exercise list — text aligns right, images stay on correct side
- [ ] Routine detail — day labels in Arabic, progress bar stays LTR
- [ ] Diet plan — meal cards flip, calorie numbers render correctly
- [ ] AI Chat — user bubbles on left, AI on right (flipped from EN)
- [ ] Notification list — text aligns right, timestamps on left
- [ ] Profile/Settings — form fields align right
- [ ] Trainer dashboard — client cards flip correctly
- [ ] Modals/Dialogs — buttons in correct order for RTL

### Font checks:
- [ ] Arabic text renders with Cairo font (not system fallback)
- [ ] Line spacing is comfortable (not cramped)
- [ ] Long Arabic text doesn't overflow containers
- [ ] Mixed Arabic + English text renders correctly

### Edge cases:
- [ ] Empty states show Arabic text
- [ ] Error toasts/snackbars show Arabic
- [ ] Pull-to-refresh label is Arabic
- [ ] Search placeholder is Arabic
- [ ] Form validation errors are Arabic
- [ ] OTP input stays LTR
- [ ] Phone number display stays LTR

---

## Common Mistakes to Avoid

1. **Forgetting `Accept-Language` header** → Backend returns English content
2. **Not calling `PATCH /api/auth/user/`** → Push notifications stay in English
3. **Using `EdgeInsets.only(left/right)`** → Layout doesn't flip in RTL
4. **Hardcoding `TextAlign.left`** → Text doesn't align correctly in Arabic
5. **Not wrapping number displays in LTR** → Calories show as "سعرة ١٥٠٠" instead of "١٥٠٠ سعرة"
6. **Translating content that comes from API** → Double translation; API already returns Arabic
7. **Using `context` in places where it's not available** → Use `Builder` or pass `AppLocalizations` down
8. **Not running `flutter gen-l10n`** → New ARB keys don't generate accessor methods
9. **Requiring app restart for language change** → Use state management properly
10. **Translating push notification body client-side** → Backend already sends it translated
