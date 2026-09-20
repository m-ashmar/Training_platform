import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'auth/auth_repository.dart';
import 'auth/token_store.dart';
import 'auth/user_cache.dart';
import 'config/env.dart';
import 'l10n/locale_controller.dart';
import 'network/api_client.dart';
import 'storage/prefs.dart';

/// Fired by the API layer when a refresh is rejected; the auth controller
/// listens and signs the user out. Keeps the network layer free of state.
class SessionEvents {
  final StreamController<void> _expired = StreamController<void>.broadcast();

  Stream<void> get expired => _expired.stream;

  void emitExpired() {
    if (!_expired.isClosed) _expired.add(null);
  }

  void dispose() {
    _expired.close();
  }
}

final sessionEventsProvider = Provider<SessionEvents>((Ref ref) {
  final SessionEvents events = SessionEvents();
  ref.onDispose(events.dispose);
  return events;
});

final tokenStoreProvider = Provider<TokenStore>((_) => SecureTokenStore());

final apiClientProvider = Provider<ApiClient>((Ref ref) {
  return ApiClient(
    baseUrl: Env.apiBaseUrl,
    tokens: ref.watch(tokenStoreProvider),
    languageCode: () => ref.read(localeControllerProvider).languageCode,
    onSessionExpired: ref.watch(sessionEventsProvider).emitExpired,
  );
});

final authRepositoryProvider = Provider<AuthRepository>(
  (Ref ref) => AuthRepository(ref.watch(apiClientProvider)),
);

final userCacheProvider = Provider<UserCache>(
  (Ref ref) => UserCache(ref.watch(sharedPreferencesProvider)),
);
