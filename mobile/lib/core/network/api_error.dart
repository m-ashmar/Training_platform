import 'dart:io';

import 'package:dio/dio.dart';

/// Stable error codes from the API contract. `detail` is translated and may
/// change; `code` is the contract. Unknown codes fall back to the HTTP status.
enum ApiCode {
  validationError('validation_error'),
  badRequest('bad_request'),
  notAuthenticated('not_authenticated'),
  tokenNotValid('token_not_valid'),
  permissionDenied('permission_denied'),
  noEntitlement('no_entitlement'),
  featureNotInPlan('feature_not_in_plan'),
  accountDisabled('account_disabled'),
  notFound('not_found'),
  methodNotAllowed('method_not_allowed'),
  rateLimited('rate_limited'),
  serverError('server_error'),
  // Client-side conditions; never on the wire.
  network(null),
  timeout(null),
  cancelled(null),
  unknown(null);

  const ApiCode(this.wire);

  final String? wire;

  static ApiCode fromWire(String? code, int? status) {
    if (code != null) {
      for (final ApiCode c in values) {
        if (c.wire == code) return c;
      }
    }
    return switch (status) {
      400 => badRequest,
      401 => notAuthenticated,
      403 => permissionDenied,
      404 => notFound,
      405 => methodNotAllowed,
      429 => rateLimited,
      final int s when s >= 500 => serverError,
      _ => unknown,
    };
  }
}

class FieldError {
  const FieldError({required this.message, required this.code});

  final String message;
  final String code;
}

/// The single failure type repositories throw. Screens switch on [code];
/// [message] is the server's translated text, used only when no local copy
/// exists for that code.
class ApiError implements Exception {
  const ApiError({
    required this.code,
    required this.message,
    this.status,
    this.wireCode,
    this.fieldErrors = const <String, List<FieldError>>{},
    this.retryAfter,
    this.requiresVerification = false,
  });

  factory ApiError.fromResponse(Response<dynamic> response) {
    final dynamic data = response.data;
    final Map<String, dynamic> body = data is Map<String, dynamic> ? data : const <String, dynamic>{};
    final String? wire = body['code'] is String ? body['code'] as String : null;
    final int? status = response.statusCode;

    final Map<String, List<FieldError>> fields = <String, List<FieldError>>{};
    final dynamic rawFields = body['field_errors'];
    if (rawFields is Map<String, dynamic>) {
      rawFields.forEach((String key, dynamic value) {
        if (value is List) {
          fields[key] = value
              .whereType<Map<String, dynamic>>()
              .map(
                (Map<String, dynamic> m) => FieldError(
                  message: m['message']?.toString() ?? '',
                  code: m['code']?.toString() ?? '',
                ),
              )
              .toList(growable: false);
        }
      });
    }

    Duration? retryAfter;
    final String? header = response.headers.value('retry-after');
    if (header != null) {
      final int? seconds = int.tryParse(header.trim());
      if (seconds != null) retryAfter = Duration(seconds: seconds);
    }

    return ApiError(
      code: ApiCode.fromWire(wire, status),
      message: (body['detail'] ?? body['error'] ?? body['message'])?.toString() ?? '',
      status: status,
      wireCode: wire,
      fieldErrors: fields,
      retryAfter: retryAfter,
      requiresVerification: body['requires_verification'] == true,
    );
  }

  factory ApiError.fromDio(DioException e) {
    final Response<dynamic>? response = e.response;
    if (response != null) return ApiError.fromResponse(response);
    return switch (e.type) {
      DioExceptionType.connectionTimeout ||
      DioExceptionType.sendTimeout ||
      DioExceptionType.receiveTimeout =>
        const ApiError(code: ApiCode.timeout, message: ''),
      DioExceptionType.cancel => const ApiError(code: ApiCode.cancelled, message: ''),
      DioExceptionType.connectionError => const ApiError(code: ApiCode.network, message: ''),
      _ => e.error is SocketException
          ? const ApiError(code: ApiCode.network, message: '')
          : ApiError(code: ApiCode.unknown, message: e.message ?? ''),
    };
  }

  final ApiCode code;

  /// Translated server text. Prefer local strings keyed on [code].
  final String message;
  final int? status;
  final String? wireCode;

  /// Present only on `validation_error`.
  final Map<String, List<FieldError>> fieldErrors;

  /// From `Retry-After` on a 429.
  final Duration? retryAfter;

  /// Login/token 400 that means "show the OTP screen", not "wrong password".
  final bool requiresVerification;

  bool get isAuthProblem => code == ApiCode.notAuthenticated || code == ApiCode.tokenNotValid;

  bool get isPaywall => code == ApiCode.noEntitlement || code == ApiCode.featureNotInPlan;

  bool get isOffline => code == ApiCode.network || code == ApiCode.timeout;

  String? fieldMessage(String field) {
    final List<FieldError>? errors = fieldErrors[field];
    if (errors == null || errors.isEmpty) return null;
    return errors.first.message;
  }

  @override
  String toString() => 'ApiError(${code.name}${status == null ? '' : ' $status'}: $message)';
}
