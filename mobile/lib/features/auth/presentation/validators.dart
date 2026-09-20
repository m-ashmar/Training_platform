import '../../../core/l10n/l10n.dart';

/// Client-side checks that mirror the server's; the server stays the truth
/// and its `field_errors` overwrite these when they arrive.
abstract final class Validators {
  static final RegExp _email = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$');

  static String? required(AppLocalizations l10n, String value) =>
      value.trim().isEmpty ? l10n.fieldRequired : null;

  static String? email(AppLocalizations l10n, String value) {
    if (value.trim().isEmpty) return l10n.fieldRequired;
    return _email.hasMatch(value.trim()) ? null : l10n.emailInvalid;
  }

  static String? password(AppLocalizations l10n, String value) {
    if (value.isEmpty) return l10n.fieldRequired;
    return value.length < 8 ? l10n.passwordTooShort : null;
  }
}
