import 'package:flutter/material.dart';

import '../tokens/palette.dart';

/// Semantic colours. Widgets read these through `context.colors`; the raw
/// [Palette] is never referenced outside the theme.
@immutable
class AppColors extends ThemeExtension<AppColors> {
  const AppColors({
    required this.bg,
    required this.surface,
    required this.surfaceRaised,
    required this.surfaceHigh,
    required this.outline,
    required this.text,
    required this.textMuted,
    required this.textFaint,
    required this.accent,
    required this.onAccent,
    required this.accentText,
    required this.trainTint,
    required this.eatTint,
    required this.success,
    required this.warning,
    required this.danger,
    required this.info,
    required this.protein,
    required this.carbs,
    required this.fat,
  });

  static const AppColors dark = AppColors(
    bg: Palette.ink0,
    surface: Palette.ink1,
    surfaceRaised: Palette.ink2,
    surfaceHigh: Palette.ink3,
    outline: Palette.ink4,
    text: Palette.textOnInk,
    textMuted: Palette.textOnInkMuted,
    textFaint: Palette.textOnInkFaint,
    accent: Palette.saffron,
    onAccent: Palette.onSaffron,
    accentText: Palette.saffron,
    trainTint: Palette.trainTintDark,
    eatTint: Palette.eatTintDark,
    success: Palette.success,
    warning: Palette.warning,
    danger: Palette.danger,
    info: Palette.info,
    protein: Palette.protein,
    carbs: Palette.carbs,
    fat: Palette.fat,
  );

  static const AppColors light = AppColors(
    bg: Palette.paper0,
    surface: Palette.paper1,
    surfaceRaised: Palette.paper1,
    surfaceHigh: Palette.paper2,
    outline: Palette.paper3,
    text: Palette.textOnPaper,
    textMuted: Palette.textOnPaperMuted,
    textFaint: Palette.textOnPaperFaint,
    accent: Palette.saffron,
    onAccent: Palette.onSaffron,
    accentText: Palette.saffronInk,
    trainTint: Palette.trainTintLight,
    eatTint: Palette.eatTintLight,
    success: Palette.successInk,
    warning: Palette.warning,
    danger: Palette.dangerInk,
    info: Palette.info,
    protein: Palette.protein,
    carbs: Palette.carbs,
    fat: Palette.fat,
  );

  /// Page background.
  final Color bg;

  /// Default container (cards, sheets, bars).
  final Color surface;

  /// Inputs and list rows sitting on a surface.
  final Color surfaceRaised;

  /// Chips, snackbars, pressed states.
  final Color surfaceHigh;

  /// Hairlines and dividers.
  final Color outline;

  final Color text;
  final Color textMuted;
  final Color textFaint;

  /// Brand fill (buttons, progress). Pair with [onAccent].
  final Color accent;
  final Color onAccent;

  /// Brand as text or icon on a surface: same hue, contrast-safe per mode.
  final Color accentText;

  /// Cool wash behind training screens.
  final Color trainTint;

  /// Warm wash behind nutrition screens.
  final Color eatTint;

  final Color success;
  final Color warning;
  final Color danger;
  final Color info;

  final Color protein;
  final Color carbs;
  final Color fat;

  @override
  AppColors copyWith({
    Color? bg,
    Color? surface,
    Color? surfaceRaised,
    Color? surfaceHigh,
    Color? outline,
    Color? text,
    Color? textMuted,
    Color? textFaint,
    Color? accent,
    Color? onAccent,
    Color? accentText,
    Color? trainTint,
    Color? eatTint,
    Color? success,
    Color? warning,
    Color? danger,
    Color? info,
    Color? protein,
    Color? carbs,
    Color? fat,
  }) {
    return AppColors(
      bg: bg ?? this.bg,
      surface: surface ?? this.surface,
      surfaceRaised: surfaceRaised ?? this.surfaceRaised,
      surfaceHigh: surfaceHigh ?? this.surfaceHigh,
      outline: outline ?? this.outline,
      text: text ?? this.text,
      textMuted: textMuted ?? this.textMuted,
      textFaint: textFaint ?? this.textFaint,
      accent: accent ?? this.accent,
      onAccent: onAccent ?? this.onAccent,
      accentText: accentText ?? this.accentText,
      trainTint: trainTint ?? this.trainTint,
      eatTint: eatTint ?? this.eatTint,
      success: success ?? this.success,
      warning: warning ?? this.warning,
      danger: danger ?? this.danger,
      info: info ?? this.info,
      protein: protein ?? this.protein,
      carbs: carbs ?? this.carbs,
      fat: fat ?? this.fat,
    );
  }

  @override
  AppColors lerp(ThemeExtension<AppColors>? other, double t) {
    if (other is! AppColors) return this;
    Color mix(Color a, Color b) => Color.lerp(a, b, t)!;
    return AppColors(
      bg: mix(bg, other.bg),
      surface: mix(surface, other.surface),
      surfaceRaised: mix(surfaceRaised, other.surfaceRaised),
      surfaceHigh: mix(surfaceHigh, other.surfaceHigh),
      outline: mix(outline, other.outline),
      text: mix(text, other.text),
      textMuted: mix(textMuted, other.textMuted),
      textFaint: mix(textFaint, other.textFaint),
      accent: mix(accent, other.accent),
      onAccent: mix(onAccent, other.onAccent),
      accentText: mix(accentText, other.accentText),
      trainTint: mix(trainTint, other.trainTint),
      eatTint: mix(eatTint, other.eatTint),
      success: mix(success, other.success),
      warning: mix(warning, other.warning),
      danger: mix(danger, other.danger),
      info: mix(info, other.info),
      protein: mix(protein, other.protein),
      carbs: mix(carbs, other.carbs),
      fat: mix(fat, other.fat),
    );
  }
}

extension AppColorsContext on BuildContext {
  AppColors get colors => Theme.of(this).extension<AppColors>()!;
  TextTheme get text => Theme.of(this).textTheme;
}
