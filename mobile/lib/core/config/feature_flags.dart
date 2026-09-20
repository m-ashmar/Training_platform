import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Areas the handoff (§4) marks as still moving. Each is off until the backend
/// fix branch lands; flipping one is a build flag, not a refactor.
///
/// `--dart-define=FF_PAYMENTS=true` etc.
final class FeatureFlags {
  const FeatureFlags({
    required this.payments,
    required this.trainerDietEditing,
    required this.challengeWrites,
    required this.routineBulkComplete,
    required this.notificationPreferences,
    required this.paymentPolling,
  });

  const FeatureFlags.fromEnvironment()
      : payments = _payments,
        trainerDietEditing = _trainerDietEditing,
        challengeWrites = _challengeWrites,
        routineBulkComplete = _routineBulkComplete,
        notificationPreferences = _notificationPreferences,
        paymentPolling = _paymentPolling;

  static const bool _payments = bool.fromEnvironment('FF_PAYMENTS');
  static const bool _trainerDietEditing = bool.fromEnvironment('FF_TRAINER_DIET_EDITING');
  static const bool _challengeWrites = bool.fromEnvironment('FF_CHALLENGE_WRITES');
  static const bool _routineBulkComplete = bool.fromEnvironment('FF_ROUTINE_BULK_COMPLETE');
  static const bool _notificationPreferences = bool.fromEnvironment('FF_NOTIFICATION_PREFERENCES');
  static const bool _paymentPolling = bool.fromEnvironment('FF_PAYMENT_POLLING', defaultValue: true);

  /// Renew, refund, pay-after-lapse.
  final bool payments;

  /// Trainer-side diet writes and meal interact (409 semantics will change).
  final bool trainerDietEditing;

  /// Challenge create/update; reads are always safe.
  final bool challengeWrites;

  /// Routine sessions bulk-complete.
  final bool routineBulkComplete;

  /// Notification preference writes ("all off" not honoured yet).
  final bool notificationPreferences;

  /// Client-side reconcile polling; a background reconciler may remove the need.
  final bool paymentPolling;
}

final featureFlagsProvider = Provider<FeatureFlags>(
  (_) => const FeatureFlags.fromEnvironment(),
);
