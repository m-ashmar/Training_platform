import '../network/api_client.dart';
import '../network/api_error.dart';
import 'app_user.dart';
import 'token_store.dart';

sealed class LoginResult {
  const LoginResult();
}

final class LoginSucceeded extends LoginResult {
  const LoginSucceeded({required this.tokens, required this.user});

  final TokenPair tokens;
  final AppUser user;
}

/// Login answered 400 `{"requires_verification": true}`: the address exists
/// but has never been verified. Show the OTP screen, not a password error.
final class LoginNeedsVerification extends LoginResult {
  const LoginNeedsVerification(this.email);

  final String email;
}

typedef Session = ({TokenPair tokens, AppUser user});

/// Wire calls for `/api/auth/`. Shapes come from FRONTEND_HANDOFF.md §3.
class AuthRepository {
  AuthRepository(this._api);

  final ApiClient _api;

  Future<LoginResult> login({required String email, required String password}) async {
    try {
      final Map<String, dynamic> json = await _api.postObject(
        '/api/auth/login/',
        body: <String, String>{'email': email, 'password': password},
        auth: false,
      );
      return LoginSucceeded(tokens: _tokens(json), user: _user(json));
    } on ApiError catch (e) {
      if (e.requiresVerification) return LoginNeedsVerification(email);
      rethrow;
    }
  }

  /// Creates the account enabled-but-unverified. The caller moves to OTP.
  Future<void> register({
    required String username,
    required String email,
    required String password,
    required String phoneNumber,
    required UserRole role,
  }) {
    return _api.postObject(
      '/api/auth/register/',
      body: <String, String>{
        'username': username,
        'email': email,
        'password1': password,
        'password2': password,
        'phone_number': phoneNumber,
        'user_type': role.wire,
      },
      auth: false,
    );
  }

  /// Any failure comes back as one message; a suspended account is the one
  /// distinguishable case (`account_disabled`).
  Future<Session> verifyOtp({required String email, required String code}) async {
    final Map<String, dynamic> json = await _api.postObject(
      '/api/auth/verify-otp/',
      body: <String, String>{'email': email, 'otp_code': code},
      auth: false,
    );
    return (tokens: _tokens(json), user: _user(json));
  }

  /// Always 200 (anti-enumeration); a 429 carries `Retry-After`.
  Future<void> resendOtp(String email) {
    return _api.postObject(
      '/api/auth/resend-otp/',
      body: <String, String>{'email': email},
      auth: false,
    );
  }

  /// Blacklists the refresh token server-side. Best effort.
  Future<void> logout(String refreshToken) {
    return _api.postObject(
      '/api/auth/token/logout/',
      body: <String, String>{'refresh_token': refreshToken},
    );
  }

  Future<AppUser> me() async {
    return AppUser.fromJson(await _api.getObject('/api/auth/user/details/'));
  }

  static TokenPair _tokens(Map<String, dynamic> json) => TokenPair(
        access: json['access'] as String,
        refresh: json['refresh'] as String,
      );

  static AppUser _user(Map<String, dynamic> json) =>
      AppUser.fromJson(json['user'] as Map<String, dynamic>);
}
