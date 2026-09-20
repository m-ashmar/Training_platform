import 'package:flutter/animation.dart';

/// 4-point grid. Use these instead of literal numbers in layout.
abstract final class Space {
  static const double xs = 4;
  static const double sm = 8;
  static const double md = 12;
  static const double lg = 16;
  static const double xl = 20;
  static const double xxl = 24;
  static const double x3 = 32;
  static const double x4 = 40;
  static const double x5 = 48;
  static const double x6 = 64;

  /// Horizontal page gutter.
  static const double gutter = 20;
}

abstract final class Radii {
  static const double sm = 8;
  static const double md = 14;
  static const double lg = 20;
  static const double xl = 28;
  static const double pill = 999;
}

/// Motion vocabulary. Four durations, two curves; nothing else in widgets.
abstract final class Motion {
  static const Duration fast = Duration(milliseconds: 120);
  static const Duration base = Duration(milliseconds: 200);
  static const Duration slow = Duration(milliseconds: 320);
  static const Duration deliberate = Duration(milliseconds: 480);

  /// Material "emphasized" easing: quick departure, soft arrival.
  static const Curve emphasized = Cubic(0.2, 0, 0, 1);
  static const Curve standard = Curves.easeOutCubic;
}

/// Minimum touch target and control heights.
abstract final class Sizes {
  static const double touchTarget = 48;
  static const double button = 56;
  static const double field = 56;
  static const double icon = 24;
}
