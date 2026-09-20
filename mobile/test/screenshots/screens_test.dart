// Design-review renders of every screen, in both languages and both modes.
// Skipped by default; produce the PNGs with:
//
//   flutter test test/screenshots --dart-define=SCREENSHOTS=true --update-goldens
//
// Output lands in test/screenshots/goldens/. Not a regression suite: Today
// shows the real date and greeting.
import 'dart:async';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:training_app/app.dart';
import 'package:training_app/core/auth/app_user.dart';
import 'package:training_app/core/auth/auth_controller.dart';
import 'package:training_app/core/auth/token_store.dart';
import 'package:training_app/core/providers.dart';
import 'package:training_app/core/router/app_router.dart';
import 'package:training_app/core/storage/prefs.dart';

const bool kScreenshots = bool.fromEnvironment('SCREENSHOTS');

const AppUser kUser = AppUser(
  id: 1,
  username: 'lina',
  email: 'lina@example.com',
  firstName: 'Lina',
  lastName: 'Haddad',
  role: UserRole.client,
);

class _SignedOut extends AuthController {
  @override
  AuthState build() => const AuthSignedOut();
}

class _Unverified extends AuthController {
  @override
  AuthState build() => const AuthUnverified('lina@example.com');
}

class _SignedIn extends AuthController {
  @override
  AuthState build() => const AuthSignedIn(kUser);
}

Future<void> loadFonts() async {
  final FontLoader loader = FontLoader('PlexArabic');
  for (final String w in <String>['Light', 'Regular', 'Medium', 'SemiBold', 'Bold']) {
    loader.addFont(rootBundle.load('assets/fonts/IBMPlexSansArabic-$w.ttf'));
  }
  await loader.load();

  // The test renderer does not ship the icon font; borrow it from the SDK.
  final String? root = Platform.environment['FLUTTER_ROOT'];
  final File icons = File('$root/bin/cache/artifacts/material_fonts/MaterialIcons-Regular.otf');
  if (icons.existsSync()) {
    final FontLoader iconLoader = FontLoader('MaterialIcons');
    iconLoader.addFont(icons.readAsBytes().then((List<int> b) => ByteData.sublistView(Uint8List.fromList(b))));
    await iconLoader.load();
  }
}

Future<ProviderContainer> containerFor({
  required String language,
  required String appearance,
  required AuthController Function() auth,
}) async {
  SharedPreferences.setMockInitialValues(<String, Object>{
    'preferred_language': language,
    'appearance': appearance,
  });
  final SharedPreferences prefs = await SharedPreferences.getInstance();
  return ProviderContainer(
    overrides: [
      sharedPreferencesProvider.overrideWithValue(prefs),
      tokenStoreProvider.overrideWithValue(InMemoryTokenStore()),
      authControllerProvider.overrideWith(auth),
    ],
  );
}

Future<void> shoot(
  WidgetTester tester,
  String name, {
  required String language,
  required String appearance,
  required AuthController Function() auth,
  String? go,
}) async {
  tester.view.physicalSize = const Size(786, 1704);
  tester.view.devicePixelRatio = 2;
  addTearDown(tester.view.reset);

  final ProviderContainer container = await containerFor(
    language: language,
    appearance: appearance,
    auth: auth,
  );
  addTearDown(container.dispose);

  await tester.pumpWidget(UncontrolledProviderScope(container: container, child: const App()));
  await tester.pumpAndSettle();
  if (go != null) {
    final GoRouter router = container.read(routerProvider);
    unawaited(router.push(go));
    await tester.pumpAndSettle();
  }
  await expectLater(find.byType(App), matchesGoldenFile('goldens/$name.png'));
}

void main() {
  if (!kScreenshots) return;

  setUpAll(loadFonts);

  testWidgets('welcome en dark', (WidgetTester t) =>
      shoot(t, 'welcome_en_dark', language: 'en', appearance: 'dark', auth: _SignedOut.new));
  testWidgets('welcome ar dark', (WidgetTester t) =>
      shoot(t, 'welcome_ar_dark', language: 'ar', appearance: 'dark', auth: _SignedOut.new));
  testWidgets('welcome ar light', (WidgetTester t) =>
      shoot(t, 'welcome_ar_light', language: 'ar', appearance: 'light', auth: _SignedOut.new));
  testWidgets('login en dark', (WidgetTester t) => shoot(t, 'login_en_dark',
      language: 'en', appearance: 'dark', auth: _SignedOut.new, go: Routes.login));
  testWidgets('login ar dark', (WidgetTester t) => shoot(t, 'login_ar_dark',
      language: 'ar', appearance: 'dark', auth: _SignedOut.new, go: Routes.login));
  testWidgets('register ar light', (WidgetTester t) => shoot(t, 'register_ar_light',
      language: 'ar', appearance: 'light', auth: _SignedOut.new, go: Routes.register));
  testWidgets('otp en dark', (WidgetTester t) =>
      shoot(t, 'otp_en_dark', language: 'en', appearance: 'dark', auth: _Unverified.new));
  testWidgets('otp ar dark', (WidgetTester t) =>
      shoot(t, 'otp_ar_dark', language: 'ar', appearance: 'dark', auth: _Unverified.new));
  testWidgets('today en dark', (WidgetTester t) =>
      shoot(t, 'today_en_dark', language: 'en', appearance: 'dark', auth: _SignedIn.new));
  testWidgets('today ar dark', (WidgetTester t) =>
      shoot(t, 'today_ar_dark', language: 'ar', appearance: 'dark', auth: _SignedIn.new));
  testWidgets('today ar light', (WidgetTester t) =>
      shoot(t, 'today_ar_light', language: 'ar', appearance: 'light', auth: _SignedIn.new));
  testWidgets('train ar dark', (WidgetTester t) => shoot(t, 'train_ar_dark',
      language: 'ar', appearance: 'dark', auth: _SignedIn.new, go: Routes.train));
  testWidgets('eat ar dark', (WidgetTester t) => shoot(t, 'eat_ar_dark',
      language: 'ar', appearance: 'dark', auth: _SignedIn.new, go: Routes.eat));
  testWidgets('me ar dark', (WidgetTester t) => shoot(t, 'me_ar_dark',
      language: 'ar', appearance: 'dark', auth: _SignedIn.new, go: Routes.me));
}
