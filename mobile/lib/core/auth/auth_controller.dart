import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../network/api_error.dart';
import '../providers.dart';
import 'app_user.dart';
import 'auth_repository.dart';
import 'token_store.dart';

sealed class AuthState {
  const AuthState();
}

/// Before `restore()` has run. Never seen by the router after bootstrap.
final class AuthUnknown extends AuthState {
  const AuthUnknown();
}

enum SignOutReason { user, sessionExpired }

final class AuthSignedOut extends AuthState {
  const AuthSignedOut({this.reason});

  final SignOutReason? reason;
}

/// Account exists, address never verified: the OTP screen owns the flow.
final class AuthUnverified extends AuthState {
  const AuthUnverified(this.email);

  final String email;
}

/// `account_disabled` from verify-otp. Terminal until support intervenes.
final class AuthSuspended extends AuthState {
  const AuthSuspended();
}

final class AuthSignedIn extends AuthState {
  const AuthSignedIn(this.user);

  final AppUser user;
}

/// The auth state machine. Screens call the methods and catch [ApiError]
/// for anything they need to show; the controller only owns transitions.
class AuthController extends Notifier<AuthState> {
  @override
  AuthState build() {
    final StreamSubscription<void> sub =
        ref.watch(sessionEventsProvider).expired.listen((_) => _onSessionExpired());
    ref.onDispose(sub.cancel);
    return const AuthUnknown();
  }

  AuthRepository get _repo => ref.read(authRepositoryProvider);
  TokenStore get _tokens => ref.read(tokenStoreProvider);

  /// Cold start. Cached user first so the first frame is already signed in;
  /// the profile refreshes behind it.
  Future<void> restore() async {
    await _tokens.load();
    if (!_tokens.hasSession) {
      state = const AuthSignedOut();
      return;
    }
    final AppUser? cached = ref.read(userCacheProvider).read();
    if (cached != null) {
      state = AuthSignedIn(cached);
      unawaited(_refreshProfile());
      return;
    }
    try {
      await _signIn(await _repo.me());
    } on ApiError catch (e) {
      if (e.isAuthProblem) {
        await _forget();
        state = const AuthSignedOut(reason: SignOutReason.sessionExpired);
      } else {
        // Tokens kept: an offline start without a cached profile falls back
        // to the welcome screen but does not destroy the session.
        state = const AuthSignedOut();
      }
    }
  }

  Future<void> login({required String email, required String password}) async {
    switch (await _repo.login(email: email, password: password)) {
      case LoginSucceeded(:final TokenPair tokens, :final AppUser user):
        await _tokens.save(tokens);
        await _signIn(user);
      case LoginNeedsVerification(:final String email):
        state = AuthUnverified(email);
    }
  }

  Future<void> register({
    required String username,
    required String email,
    required String password,
    required String phoneNumber,
    required UserRole role,
  }) async {
    await _repo.register(
      username: username,
      email: email,
      password: password,
      phoneNumber: phoneNumber,
      role: role,
    );
    state = AuthUnverified(email);
  }

  /// Throws [ApiError] for a rejected code; moves to [AuthSuspended] itself.
  Future<void> verifyOtp(String code) async {
    final AuthState current = state;
    if (current is! AuthUnverified) return;
    try {
      final Session session = await _repo.verifyOtp(email: current.email, code: code);
      await _tokens.save(session.tokens);
      await _signIn(session.user);
    } on ApiError catch (e) {
      if (e.code == ApiCode.accountDisabled) {
        state = const AuthSuspended();
        return;
      }
      rethrow;
    }
  }

  Future<void> resendOtp() async {
    final AuthState current = state;
    if (current is! AuthUnverified) return;
    await _repo.resendOtp(current.email);
  }

  /// From the OTP or suspended screen back to the start.
  void leaveVerification() {
    state = const AuthSignedOut();
  }

  Future<void> signOut() async {
    final String? refresh = _tokens.refresh;
    if (refresh != null) {
      try {
        await _repo.logout(refresh);
      } on ApiError {
        // The local session ends regardless.
      }
    }
    await _forget();
    state = const AuthSignedOut(reason: SignOutReason.user);
  }

  Future<void> _signIn(AppUser user) async {
    await ref.read(userCacheProvider).write(user);
    state = AuthSignedIn(user);
  }

  Future<void> _refreshProfile() async {
    try {
      final AppUser user = await _repo.me();
      if (state is AuthSignedIn) await _signIn(user);
    } on ApiError {
      // Session expiry arrives through SessionEvents; anything else waits
      // for the next foreground refresh.
    }
  }

  Future<void> _onSessionExpired() async {
    if (state is! AuthSignedIn) return;
    await _forget();
    state = const AuthSignedOut(reason: SignOutReason.sessionExpired);
  }

  Future<void> _forget() async {
    await _tokens.clear();
    await ref.read(userCacheProvider).clear();
  }
}

final authControllerProvider = NotifierProvider<AuthController, AuthState>(AuthController.new);
