import 'package:dio/dio.dart';

import '../../auth/token_store.dart';

/// Bearer header on the way out; on a 401, one refresh and one retry.
///
/// A [QueuedInterceptor] runs its callbacks one at a time, so concurrent 401s
/// line up behind the first refresh and simply retry with the new token
/// (rotation blacklists the old refresh token the moment it is used, so two
/// refreshes in flight would sign the user out).
class AuthInterceptor extends QueuedInterceptor {
  AuthInterceptor({
    required TokenStore tokens,
    required Future<TokenPair?> Function(String refreshToken) refresh,
    required Future<Response<dynamic>> Function(RequestOptions options) retry,
    required void Function() onSessionExpired,
  })  : _tokens = tokens,
        _refresh = refresh,
        _retry = retry,
        _onSessionExpired = onSessionExpired;

  /// `options.extra[skipAuth] = true` for public endpoints.
  static const String skipAuth = 'skipAuth';
  static const String _retried = 'authRetried';

  final TokenStore _tokens;
  final Future<TokenPair?> Function(String refreshToken) _refresh;
  final Future<Response<dynamic>> Function(RequestOptions options) _retry;
  final void Function() _onSessionExpired;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    if (options.extra[skipAuth] != true) {
      final String? access = _tokens.access;
      if (access != null) options.headers['Authorization'] = 'Bearer $access';
    }
    handler.next(options);
  }

  @override
  Future<void> onError(DioException err, ErrorInterceptorHandler handler) async {
    final RequestOptions options = err.requestOptions;
    final bool eligible = err.response?.statusCode == 401 &&
        options.extra[skipAuth] != true &&
        options.extra[_retried] != true;
    if (!eligible) return handler.next(err);

    final String? sent = _bearer(options);
    String? access = _tokens.access;

    // Someone ahead of us in the queue already refreshed: reuse their token.
    if (access == null || access == sent) {
      final String? refreshToken = _tokens.refresh;
      if (refreshToken == null) {
        _onSessionExpired();
        return handler.next(err);
      }
      final TokenPair? pair;
      try {
        pair = await _refresh(refreshToken);
      } on DioException {
        // Network trouble during refresh is not a dead session; surface the
        // original failure and let the next call try again.
        return handler.next(err);
      }
      if (pair == null) {
        await _tokens.clear();
        _onSessionExpired();
        return handler.next(err);
      }
      await _tokens.save(pair);
      access = pair.access;
    }

    options.headers['Authorization'] = 'Bearer $access';
    options.extra[_retried] = true;
    try {
      handler.resolve(await _retry(options));
    } on DioException catch (e) {
      handler.next(e);
    }
  }

  static String? _bearer(RequestOptions options) {
    final Object? header = options.headers['Authorization'];
    if (header is! String || !header.startsWith('Bearer ')) return null;
    return header.substring('Bearer '.length);
  }
}
