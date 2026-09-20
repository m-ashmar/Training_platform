import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';

import '../auth/token_store.dart';
import 'api_error.dart';
import 'interceptors/auth_interceptor.dart';
import 'interceptors/language_interceptor.dart';

/// The only HTTP client in the app. Base URL, bearer header, refresh-on-401,
/// `Accept-Language` and error decoding all live here; nothing else imports
/// `dio` except the interceptors and tests.
class ApiClient {
  ApiClient({
    required String baseUrl,
    required TokenStore tokens,
    required String Function() languageCode,
    required void Function() onSessionExpired,
    Dio? dio,
    Dio? refreshDio,
    bool? logging,
  })  : _dio = dio ?? Dio(),
        _bare = refreshDio ?? Dio() {
    _configure(_dio, baseUrl);
    _configure(_bare, baseUrl);
    _bare.interceptors.add(LanguageInterceptor(languageCode));
    _dio.interceptors.addAll(<Interceptor>[
      LanguageInterceptor(languageCode),
      AuthInterceptor(
        tokens: tokens,
        refresh: _refreshTokens,
        retry: (RequestOptions o) => _dio.fetch<dynamic>(o),
        onSessionExpired: onSessionExpired,
      ),
      if (logging ?? kDebugMode) LogInterceptor(requestBody: true, responseBody: false),
    ]);
  }

  static const String refreshPath = '/api/auth/token/refresh/';

  final Dio _dio;

  /// Interceptor-free client for the refresh call itself.
  final Dio _bare;

  /// Applied to injected clients too, so tests and production share one
  /// transport configuration.
  static void _configure(Dio dio, String baseUrl) {
    dio.options
      ..baseUrl = baseUrl
      ..connectTimeout = const Duration(seconds: 10)
      ..sendTimeout = const Duration(seconds: 20)
      ..receiveTimeout = const Duration(seconds: 20)
      ..contentType = Headers.jsonContentType
      ..responseType = ResponseType.json
      ..headers['Accept'] = 'application/json';
  }

  /// Returns null when the refresh token is rejected (rotated, blacklisted,
  /// expired); throws [DioException] on transport failure.
  Future<TokenPair?> _refreshTokens(String refreshToken) async {
    try {
      final Response<dynamic> r = await _bare.post<dynamic>(
        refreshPath,
        data: <String, String>{'refresh': refreshToken},
      );
      final Map<String, dynamic> body = _asObject(r.data);
      final String? access = body['access'] as String?;
      if (access == null) return null;
      // Rotation is on: the response carries a new refresh token that must
      // replace the one just spent.
      return TokenPair(access: access, refresh: body['refresh'] as String? ?? refreshToken);
    } on DioException catch (e) {
      final int? status = e.response?.statusCode;
      if (status == 400 || status == 401 || status == 403) return null;
      rethrow;
    }
  }

  Future<Map<String, dynamic>> getObject(
    String path, {
    Map<String, dynamic>? query,
    bool auth = true,
  }) {
    return guard(() async {
      final Response<dynamic> r = await _dio.get<dynamic>(
        path,
        queryParameters: query,
        options: _opts(auth),
      );
      return _asObject(r.data);
    });
  }

  Future<Map<String, dynamic>> postObject(
    String path, {
    Object? body,
    bool auth = true,
  }) {
    return guard(() async {
      final Response<dynamic> r = await _dio.post<dynamic>(path, data: body, options: _opts(auth));
      return _asObject(r.data);
    });
  }

  Future<Map<String, dynamic>> patchObject(
    String path, {
    Object? body,
    bool auth = true,
  }) {
    return guard(() async {
      final Response<dynamic> r = await _dio.patch<dynamic>(path, data: body, options: _opts(auth));
      return _asObject(r.data);
    });
  }

  Future<void> delete(String path, {Object? body, bool auth = true}) {
    return guard(() async {
      await _dio.delete<dynamic>(path, data: body, options: _opts(auth));
    });
  }

  static Options _opts(bool auth) => Options(
        extra: <String, Object?>{if (!auth) AuthInterceptor.skipAuth: true},
      );

  static Map<String, dynamic> _asObject(dynamic data) {
    if (data is Map<String, dynamic>) return data;
    if (data == null || data == '') return const <String, dynamic>{};
    return <String, dynamic>{'value': data};
  }

  /// Every call goes through here so a [DioException] never escapes as such.
  static Future<T> guard<T>(Future<T> Function() call) async {
    try {
      return await call();
    } on DioException catch (e) {
      throw ApiError.fromDio(e);
    }
  }
}
