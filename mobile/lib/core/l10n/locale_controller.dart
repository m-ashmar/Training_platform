import 'dart:ui';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/auth_controller.dart';
import '../network/api_error.dart';
import '../providers.dart';
import '../storage/prefs.dart';

/// App language. Persisted locally, sent as `Accept-Language` on every
/// request, and mirrored to the profile so pushes and emails follow it.
class LocaleController extends Notifier<Locale> {
  static const String _key = 'preferred_language';
  static const List<String> supported = <String>['en', 'ar'];

  @override
  Locale build() {
    final String? saved = ref.watch(sharedPreferencesProvider).getString(_key);
    if (saved != null && supported.contains(saved)) return Locale(saved);
    final String device = PlatformDispatcher.instance.locale.languageCode;
    return Locale(supported.contains(device) ? device : 'en');
  }

  String get languageCode => state.languageCode;

  bool get isArabic => state.languageCode == 'ar';

  Future<void> set(String code) async {
    if (!supported.contains(code) || code == state.languageCode) return;
    state = Locale(code);
    await ref.read(sharedPreferencesProvider).setString(_key, code);
    if (ref.read(authControllerProvider) is AuthSignedIn) {
      try {
        await ref.read(apiClientProvider).postObject(
          '/api/auth/user/update/',
          body: <String, String>{'preferred_language': code},
        );
      } on ApiError {
        // Best effort: the header already carries the language for API
        // replies; only async messages lag until the next successful sync.
      }
    }
  }
}

final localeControllerProvider = NotifierProvider<LocaleController, Locale>(LocaleController.new);
