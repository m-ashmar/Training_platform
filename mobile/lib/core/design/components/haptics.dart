import 'dart:async';

import 'package:flutter/services.dart';

/// Four intents, so haptics stay consistent across the app. Fire-and-forget:
/// a haptic never gates the interaction it decorates.
abstract final class Haptics {
  static void tap() => unawaited(HapticFeedback.lightImpact());
  static void success() => unawaited(HapticFeedback.mediumImpact());
  static void error() => unawaited(HapticFeedback.heavyImpact());
  static void tick() => unawaited(HapticFeedback.selectionClick());
}
