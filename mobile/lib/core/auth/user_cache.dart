import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import 'app_user.dart';

/// Last known user, so a cold start with valid tokens renders immediately
/// and an offline start still knows who is signed in.
class UserCache {
  UserCache(this._prefs);

  static const String _key = 'auth.user';

  final SharedPreferences _prefs;

  AppUser? read() {
    final String? raw = _prefs.getString(_key);
    if (raw == null) return null;
    try {
      return AppUser.fromJson(jsonDecode(raw) as Map<String, dynamic>);
    } on Object {
      return null;
    }
  }

  Future<void> write(AppUser user) => _prefs.setString(_key, jsonEncode(user.toJson()));

  Future<void> clear() => _prefs.remove(_key);
}
