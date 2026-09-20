import 'package:flutter/material.dart';

/// Raw colour values. Never used directly by widgets: widgets read the
/// semantic [AppColors] theme extension, which maps these per brightness.
abstract final class Palette {
  // Ink: dark surfaces, stepped by elevation.
  static const Color ink0 = Color(0xFF0B0D10);
  static const Color ink1 = Color(0xFF12151A);
  static const Color ink2 = Color(0xFF1A1E25);
  static const Color ink3 = Color(0xFF242932);
  static const Color ink4 = Color(0xFF303640);

  // Paper: light surfaces, warm off-white rather than clinical white.
  static const Color paper0 = Color(0xFFF6F5F2);
  static const Color paper1 = Color(0xFFFFFFFF);
  static const Color paper2 = Color(0xFFEEECE7);
  static const Color paper3 = Color(0xFFE3E0DA);

  // Text.
  static const Color textOnInk = Color(0xFFF3F4F6);
  static const Color textOnInkMuted = Color(0xFF9AA3B2);
  static const Color textOnInkFaint = Color(0xFF5C6673);
  static const Color textOnPaper = Color(0xFF15181D);
  static const Color textOnPaperMuted = Color(0xFF5E6672);
  static const Color textOnPaperFaint = Color(0xFF9AA1AB);

  // Brand accent: saffron. 10.9:1 on ink0, so it carries text on dark.
  static const Color saffron = Color(0xFFF2C14E);
  static const Color onSaffron = Color(0xFF1B1400);

  /// Saffron is too light to be text on paper; this is the accent for
  /// links, icons and labels in light mode (6.4:1 on white).
  static const Color saffronInk = Color(0xFF7A5A0E);

  // Domain temperature: the app cools for training and warms for nutrition.
  static const Color trainTintDark = Color(0xFF141C27);
  static const Color eatTintDark = Color(0xFF221A15);
  static const Color trainTintLight = Color(0xFFEAF0F7);
  static const Color eatTintLight = Color(0xFFF8EFE6);

  // Semantic.
  static const Color success = Color(0xFF3DDC97);
  static const Color warning = Color(0xFFFF9F1C);
  static const Color danger = Color(0xFFFF5C5C);
  static const Color info = Color(0xFF5DA9FF);
  static const Color successInk = Color(0xFF0F7A50);
  static const Color dangerInk = Color(0xFFB42323);

  // Macros: fixed across themes so a ring means the same thing everywhere.
  static const Color protein = Color(0xFF5B8CFF);
  static const Color carbs = Color(0xFFE9C46A);
  static const Color fat = Color(0xFFFF7A59);
}
