import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:training_app/core/auth/token_store.dart';
import 'package:training_app/core/network/api_client.dart';
import 'package:training_app/core/network/api_error.dart';

/// Scripted HTTP: answers by path and bearer token, counts refresh calls.
class FakeServer implements HttpClientAdapter {
  FakeServer({this.refreshOutcome = 200});

  int refreshCalls = 0;
  int refreshOutcome;
  final List<String?> bearersSeen = <String?>[];
  static const String validAccess = 'access-new';
  static const String validRefresh = 'refresh-1';

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? body, Future<void>? cancel) async {
    if (options.path == ApiClient.refreshPath) {
      refreshCalls++;
      if (refreshOutcome != 200) {
        return _json(refreshOutcome, <String, String>{'code': 'token_not_valid', 'detail': 'dead'});
      }
      return _json(200, <String, String>{'access': validAccess, 'refresh': 'refresh-2'});
    }
    final String? bearer = (options.headers['Authorization'] as String?)?.replaceFirst('Bearer ', '');
    bearersSeen.add(bearer);
    if (options.path.startsWith('/public')) return _json(200, <String, bool>{'ok': true});
    if (bearer == validAccess) return _json(200, <String, String>{'id': '1'});
    return _json(401, <String, String>{'code': 'token_not_valid', 'detail': 'expired'});
  }

  static ResponseBody _json(int status, Object body) => ResponseBody.fromString(
        jsonEncode(body),
        status,
        headers: <String, List<String>>{
          Headers.contentTypeHeader: <String>[Headers.jsonContentType],
        },
      );

  @override
  void close({bool force = false}) {}
}

ApiClient client(FakeServer server, TokenStore tokens, void Function() onExpired) {
  Dio dio() => Dio(BaseOptions(baseUrl: 'http://test'))..httpClientAdapter = server;
  return ApiClient(
    baseUrl: 'http://test',
    tokens: tokens,
    languageCode: () => 'ar',
    onSessionExpired: onExpired,
    dio: dio(),
    refreshDio: dio(),
    logging: false,
  );
}

void main() {
  test('an expired access token is refreshed once and the request retried', () async {
    final FakeServer server = FakeServer();
    final InMemoryTokenStore tokens =
        InMemoryTokenStore(initial: const TokenPair(access: 'access-old', refresh: FakeServer.validRefresh));
    int expired = 0;
    final ApiClient api = client(server, tokens, () => expired++);

    final Map<String, dynamic> body = await api.getObject('/api/auth/user/details/');

    expect(body['id'], '1');
    expect(server.refreshCalls, 1);
    expect(server.bearersSeen, <String>['access-old', FakeServer.validAccess]);
    expect(tokens.access, FakeServer.validAccess);
    expect(tokens.refresh, 'refresh-2', reason: 'rotation: the new refresh token must replace the spent one');
    expect(expired, 0);
  });

  test('concurrent 401s share a single refresh', () async {
    final FakeServer server = FakeServer();
    final InMemoryTokenStore tokens =
        InMemoryTokenStore(initial: const TokenPair(access: 'access-old', refresh: FakeServer.validRefresh));
    final ApiClient api = client(server, tokens, () {});

    final List<Map<String, dynamic>> results = await Future.wait(<Future<Map<String, dynamic>>>[
      api.getObject('/api/a/'),
      api.getObject('/api/b/'),
      api.getObject('/api/c/'),
    ]);

    expect(results.every((Map<String, dynamic> r) => r['id'] == '1'), isTrue);
    expect(server.refreshCalls, 1);
  });

  test('a rejected refresh clears the session and reports it once', () async {
    final FakeServer server = FakeServer(refreshOutcome: 401);
    final InMemoryTokenStore tokens =
        InMemoryTokenStore(initial: const TokenPair(access: 'access-old', refresh: FakeServer.validRefresh));
    int expired = 0;
    final ApiClient api = client(server, tokens, () => expired++);

    await expectLater(
      api.getObject('/api/auth/user/details/'),
      throwsA(isA<ApiError>().having((ApiError e) => e.code, 'code', ApiCode.tokenNotValid)),
    );
    expect(tokens.hasSession, isFalse);
    expect(expired, 1);
    expect(server.refreshCalls, 1);
  });

  test('public calls carry no bearer, send Accept-Language, and never refresh', () async {
    final FakeServer server = FakeServer();
    final InMemoryTokenStore tokens =
        InMemoryTokenStore(initial: const TokenPair(access: 'access-old', refresh: FakeServer.validRefresh));
    final ApiClient api = client(server, tokens, () {});

    await api.postObject('/public/login/', body: <String, String>{'email': 'a@b.c'}, auth: false);

    expect(server.bearersSeen, <String?>[null]);
    expect(server.refreshCalls, 0);
  });

  test('every request sends Accept-Language from the current locale', () async {
    late RequestOptions seen;
    final Dio dio = Dio(BaseOptions(baseUrl: 'http://test'))
      ..httpClientAdapter = _Capture((RequestOptions o) => seen = o);
    final ApiClient api = ApiClient(
      baseUrl: 'http://test',
      tokens: InMemoryTokenStore(),
      languageCode: () => 'ar',
      onSessionExpired: () {},
      dio: dio,
      refreshDio: dio,
      logging: false,
    );

    await api.getObject('/api/anything/', auth: false);

    expect(seen.headers['Accept-Language'], 'ar');
    expect(seen.headers['Accept'], 'application/json');
  });
}

class _Capture implements HttpClientAdapter {
  _Capture(this.onRequest);

  final void Function(RequestOptions) onRequest;

  @override
  Future<ResponseBody> fetch(RequestOptions options, Stream<Uint8List>? body, Future<void>? cancel) async {
    onRequest(options);
    return ResponseBody.fromString(
      '{}',
      200,
      headers: <String, List<String>>{
        Headers.contentTypeHeader: <String>[Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
