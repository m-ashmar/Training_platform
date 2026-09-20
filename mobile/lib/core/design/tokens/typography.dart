import 'package:flutter/material.dart';

/// The one font family, both scripts.
const String kFontFamily = 'PlexArabic';

/// Tabular figures so digits line up in set logs, timers and macro tables.
const List<FontFeature> kTabularFigures = <FontFeature>[FontFeature.tabularFigures()];

/// Builds the type scale. Arabic glyphs sit taller and want ~10% more leading;
/// everything else is shared, so mixed-script lines keep one rhythm.
TextTheme buildTextTheme({required bool arabic, required Color color, required Color muted}) {
  final double lead = arabic ? 1.1 : 1.0;
  final double display = arabic ? -0.5 : -1.0;

  TextStyle style(
    double size,
    double height,
    FontWeight weight, {
    double letterSpacing = 0,
    Color? c,
  }) {
    return TextStyle(
      fontFamily: kFontFamily,
      fontSize: size,
      height: height * lead,
      fontWeight: weight,
      letterSpacing: letterSpacing,
      color: c ?? color,
    );
  }

  return TextTheme(
    displayLarge: style(56, 1.05, FontWeight.w300, letterSpacing: display),
    displayMedium: style(44, 1.1, FontWeight.w300, letterSpacing: display),
    displaySmall: style(36, 1.15, FontWeight.w400, letterSpacing: display / 2),
    headlineLarge: style(32, 1.2, FontWeight.w600, letterSpacing: -0.4),
    headlineMedium: style(28, 1.2, FontWeight.w600, letterSpacing: -0.3),
    headlineSmall: style(24, 1.25, FontWeight.w600, letterSpacing: -0.2),
    titleLarge: style(20, 1.3, FontWeight.w600),
    titleMedium: style(17, 1.35, FontWeight.w600),
    titleSmall: style(15, 1.35, FontWeight.w600),
    bodyLarge: style(17, 1.5, FontWeight.w400),
    bodyMedium: style(15, 1.5, FontWeight.w400),
    bodySmall: style(13, 1.45, FontWeight.w400, c: muted),
    labelLarge: style(15, 1.2, FontWeight.w600, letterSpacing: 0.1),
    labelMedium: style(13, 1.2, FontWeight.w500, letterSpacing: 0.2, c: muted),
    labelSmall: style(11, 1.2, FontWeight.w500, letterSpacing: 0.4, c: muted),
  );
}

/// Extra styles that Material's scale has no slot for.
extension AppTextStyles on TextTheme {
  /// Big numbers: weight, reps, calories, timers.
  TextStyle get numeralLarge => displayMedium!.copyWith(
        fontWeight: FontWeight.w500,
        fontFeatures: kTabularFigures,
        letterSpacing: -1.5,
      );

  TextStyle get numeralMedium => headlineMedium!.copyWith(
        fontWeight: FontWeight.w500,
        fontFeatures: kTabularFigures,
        letterSpacing: -0.5,
      );

  TextStyle get numeralSmall => titleMedium!.copyWith(
        fontFeatures: kTabularFigures,
      );

  /// Section eyebrow: small caps feel without small caps.
  TextStyle get eyebrow => labelSmall!.copyWith(letterSpacing: 1.2);
}
