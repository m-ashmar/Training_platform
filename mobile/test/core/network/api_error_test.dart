import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:training_app/core/network/api_error.dart';

Response<dynamic> res(int status, Object? data, {Map<String, List<String>>? headers}) {
  return Response<dynamic>(
    requestOptions: RequestOptions(path: '/x'),
    statusCode: status,
    data: data,
    headers: Headers.fromMap(headers ?? <String, List<String>>{}),
  );
}

void main() {
  group('ApiError.fromResponse', () {
    test('decodes validation_error with field_errors', () {
      final ApiError e = ApiError.fromResponse(
        res(400, <String, Object>{
          'detail': 'Enter a valid email address.',
          'error': 'Enter a valid email address.',
          'code': 'validation_error',
          'field_errors': <String, Object>{
            'email': <Object>[
              <String, String>{'message': 'Enter a valid email address.', 'code': 'invalid'},
            ],
          },
        }),
      );
      expect(e.code, ApiCode.validationError);
      expect(e.fieldMessage('email'), 'Enter a valid email address.');
      expect(e.fieldMessage('password'), isNull);
      expect(e.message, 'Enter a valid email address.');
    });

    test('an unknown code falls back to the HTTP status default', () {
      expect(
        ApiError.fromResponse(res(403, <String, String>{'code': 'brand_new_code', 'detail': 'x'})).code,
        ApiCode.permissionDenied,
      );
      expect(ApiError.fromResponse(res(503, 'gateway down')).code, ApiCode.serverError);
      expect(ApiError.fromResponse(res(404, null)).code, ApiCode.notFound);
    });

    test('paywall codes are distinct from permission_denied', () {
      expect(ApiError.fromResponse(res(403, <String, String>{'code': 'no_entitlement'})).isPaywall, isTrue);
      expect(
        ApiError.fromResponse(res(403, <String, String>{'code': 'feature_not_in_plan'})).code,
        ApiCode.featureNotInPlan,
      );
      expect(
        ApiError.fromResponse(res(403, <String, String>{'code': 'permission_denied'})).isPaywall,
        isFalse,
      );
    });

    test('login 400 with requires_verification is flagged, not a validation error', () {
      final ApiError e = ApiError.fromResponse(
        res(400, <String, Object>{'requires_verification': true, 'detail': 'verify first'}),
      );
      expect(e.requiresVerification, isTrue);
      expect(e.code, ApiCode.badRequest);
    });

    test('429 carries Retry-After', () {
      final ApiError e = ApiError.fromResponse(
        res(
          429,
          <String, String>{'code': 'rate_limited'},
          headers: <String, List<String>>{
            'retry-after': <String>['120'],
          },
        ),
      );
      expect(e.code, ApiCode.rateLimited);
      expect(e.retryAfter, const Duration(seconds: 120));
    });

    test('account_disabled is decoded from the legacy error key', () {
      final ApiError e = ApiError.fromResponse(
        res(403, <String, String>{'error': 'This account is not available.', 'code': 'account_disabled'}),
      );
      expect(e.code, ApiCode.accountDisabled);
      expect(e.message, 'This account is not available.');
    });
  });

  group('ApiError.fromDio', () {
    test('maps transport failures to client-side codes', () {
      final RequestOptions ro = RequestOptions(path: '/x');
      expect(
        ApiError.fromDio(DioException(requestOptions: ro, type: DioExceptionType.connectionTimeout)).code,
        ApiCode.timeout,
      );
      expect(
        ApiError.fromDio(DioException(requestOptions: ro, type: DioExceptionType.connectionError)).code,
        ApiCode.network,
      );
      expect(
        ApiError.fromDio(DioException(requestOptions: ro, type: DioExceptionType.cancel)).code,
        ApiCode.cancelled,
      );
    });
  });
}
