import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:training_app/app.dart';
import 'package:training_app/core/auth/auth_controller.dart';
import 'package:training_app/core/auth/token_store.dart';
import 'package:training_app/core/providers.dart';
import 'package:training_app/core/storage/prefs.dart';
import 'package:training_app/features/auth/presentation/welcome_screen.dart';

Future<ProviderContainer> containerWith({required String language}) async {
  SharedPreferences.setMockInitialValues(<String, Object>{'preferred_language': language});
  final SharedPreferences prefs = await SharedPreferences.getInstance();
  final ProviderContainer container = ProviderContainer(
    overrides: [
      sharedPreferencesProvider.overrideWithValue(prefs),
      tokenStoreProvider.overrideWithValue(InMemoryTokenStore()),
    ],
  );
  await container.read(authControllerProvider.notifier).restore();
  return container;
}

void main() {
  testWidgets('Arabic preference opens the welcome screen right-to-left', (WidgetTester tester) async {
    final ProviderContainer container = await containerWith(language: 'ar');
    addTearDown(container.dispose);

    await tester.pumpWidget(UncontrolledProviderScope(container: container, child: const App()));
    await tester.pumpAndSettle();

    expect(find.byType(WelcomeScreen), findsOneWidget);
    expect(find.text('تسجيل الدخول'), findsOneWidget);
    expect(Directionality.of(tester.element(find.byType(WelcomeScreen))), TextDirection.rtl);
    expect(Theme.of(tester.element(find.byType(WelcomeScreen))).textTheme.bodyLarge?.fontFamily, 'PlexArabic');
  });

  testWidgets('the language chip switches to English without a restart', (WidgetTester tester) async {
    final ProviderContainer container = await containerWith(language: 'ar');
    addTearDown(container.dispose);

    await tester.pumpWidget(UncontrolledProviderScope(container: container, child: const App()));
    await tester.pumpAndSettle();
    await tester.tap(find.text('English'));
    await tester.pumpAndSettle();

    expect(find.text('Sign in'), findsOneWidget);
    expect(Directionality.of(tester.element(find.byType(WelcomeScreen))), TextDirection.ltr);
    expect((await SharedPreferences.getInstance()).getString('preferred_language'), 'en');
  });

  testWidgets('no session lands on welcome, never on a tab', (WidgetTester tester) async {
    final ProviderContainer container = await containerWith(language: 'en');
    addTearDown(container.dispose);

    await tester.pumpWidget(UncontrolledProviderScope(container: container, child: const App()));
    await tester.pumpAndSettle();

    expect(container.read(authControllerProvider), isA<AuthSignedOut>());
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.byType(WelcomeScreen), findsOneWidget);
  });
}
