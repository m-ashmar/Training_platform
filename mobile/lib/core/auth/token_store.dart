import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class TokenPair {
  const TokenPair({required this.access, required this.refresh});

  final String access;
  final String refresh;
}

/// Tokens are read synchronously by the interceptor, so the store keeps an
/// in-memory copy and persists behind it.
abstract class TokenStore {
  String? get access;
  String? get refresh;

  bool get hasSession => refresh != null;

  Future<void> load();
  Future<void> save(TokenPair pair);
  Future<void> clear();
}

class SecureTokenStore implements TokenStore {
  SecureTokenStore({FlutterSecureStorage? storage})
      : _storage = storage ?? const FlutterSecureStorage();

  static const String _accessKey = 'auth.access';
  static const String _refreshKey = 'auth.refresh';

  final FlutterSecureStorage _storage;
  String? _access;
  String? _refresh;

  @override
  String? get access => _access;

  @override
  String? get refresh => _refresh;

  @override
  bool get hasSession => _refresh != null;

  @override
  Future<void> load() async {
    _access = await _storage.read(key: _accessKey);
    _refresh = await _storage.read(key: _refreshKey);
  }

  @override
  Future<void> save(TokenPair pair) async {
    _access = pair.access;
    _refresh = pair.refresh;
    await _storage.write(key: _accessKey, value: pair.access);
    await _storage.write(key: _refreshKey, value: pair.refresh);
  }

  @override
  Future<void> clear() async {
    _access = null;
    _refresh = null;
    await _storage.delete(key: _accessKey);
    await _storage.delete(key: _refreshKey);
  }
}

class InMemoryTokenStore implements TokenStore {
  InMemoryTokenStore({TokenPair? initial}) {
    if (initial != null) {
      access = initial.access;
      refresh = initial.refresh;
    }
  }

  @override
  String? access;

  @override
  String? refresh;

  @override
  bool get hasSession => refresh != null;

  @override
  Future<void> load() async {}

  @override
  Future<void> save(TokenPair pair) async {
    access = pair.access;
    refresh = pair.refresh;
  }

  @override
  Future<void> clear() async {
    access = null;
    refresh = null;
  }
}
