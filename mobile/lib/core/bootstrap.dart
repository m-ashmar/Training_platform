import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'auth/auth_controller.dart';
import 'l10n/locale_controller.dart';
import 'storage/prefs.dart';

/// Everything the first frame needs, resolved before `runApp`: preferences,
/// the saved language and the auth state. The splash route is only a
/// fallback; in practice the app opens straight on the right screen.
Future<ProviderContainer> bootstrap() async {
  WidgetsFlutterBinding.ensureInitialized();
  await SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  await SystemChrome.setPreferredOrientations(<DeviceOrientation>[DeviceOrientation.portraitUp]);

  final SharedPreferences prefs = await SharedPreferences.getInstance();
  final ProviderContainer container = ProviderContainer(
    overrides: [sharedPreferencesProvider.overrideWithValue(prefs)],
  );
  container.read(localeControllerProvider);
  await container.read(authControllerProvider.notifier).restore();
  return container;
}
