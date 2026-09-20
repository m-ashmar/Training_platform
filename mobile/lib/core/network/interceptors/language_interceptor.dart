import 'package:dio/dio.dart';

/// `Accept-Language` on every request, read at send time so a language
/// switch applies to the very next call.
class LanguageInterceptor extends Interceptor {
  LanguageInterceptor(this._languageCode);

  final String Function() _languageCode;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    options.headers['Accept-Language'] = _languageCode();
    handler.next(options);
  }
}
