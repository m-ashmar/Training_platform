import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../tokens/palette.dart';
import '../tokens/spacing.dart';
import '../tokens/typography.dart';
import 'app_colors.dart';

/// Builds the two themes from the token set. The locale is an input because
/// Arabic needs its own leading; nothing else varies by language.
abstract final class AppTheme {
  static ThemeData dark({required bool arabic}) => _build(Brightness.dark, arabic: arabic);

  static ThemeData light({required bool arabic}) => _build(Brightness.light, arabic: arabic);

  static ThemeData _build(Brightness brightness, {required bool arabic}) {
    final bool isDark = brightness == Brightness.dark;
    final AppColors c = isDark ? AppColors.dark : AppColors.light;
    final TextTheme text = buildTextTheme(arabic: arabic, color: c.text, muted: c.textMuted);

    final ColorScheme scheme = ColorScheme(
      brightness: brightness,
      primary: c.accent,
      onPrimary: c.onAccent,
      primaryContainer: c.accent.withValues(alpha: 0.16),
      onPrimaryContainer: c.accentText,
      secondary: c.info,
      onSecondary: Palette.ink0,
      error: c.danger,
      onError: isDark ? Palette.ink0 : Palette.paper1,
      surface: c.surface,
      onSurface: c.text,
      surfaceContainerLowest: c.bg,
      surfaceContainerLow: c.surface,
      surfaceContainer: c.surfaceRaised,
      surfaceContainerHigh: c.surfaceHigh,
      surfaceContainerHighest: c.surfaceHigh,
      onSurfaceVariant: c.textMuted,
      outline: c.outline,
      outlineVariant: c.outline,
      inverseSurface: isDark ? Palette.paper1 : Palette.ink1,
      onInverseSurface: isDark ? Palette.textOnPaper : Palette.textOnInk,
    );

    RoundedRectangleBorder rounded(double r) =>
        RoundedRectangleBorder(borderRadius: BorderRadius.circular(r));

    OutlineInputBorder inputBorder(Color color, {double width = 1}) => OutlineInputBorder(
          borderRadius: BorderRadius.circular(Radii.md),
          borderSide: BorderSide(color: color, width: width),
        );

    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      extensions: <ThemeExtension<dynamic>>[c],
      fontFamily: kFontFamily,
      textTheme: text,
      scaffoldBackgroundColor: c.bg,
      canvasColor: c.bg,
      splashFactory: InkRipple.splashFactory,
      visualDensity: VisualDensity.standard,
      iconTheme: IconThemeData(color: c.text, size: Sizes.icon),
      dividerTheme: DividerThemeData(color: c.outline, thickness: 1, space: 1),
      appBarTheme: AppBarTheme(
        backgroundColor: Colors.transparent,
        surfaceTintColor: Colors.transparent,
        foregroundColor: c.text,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleTextStyle: text.titleLarge,
        systemOverlayStyle: isDark ? SystemUiOverlayStyle.light : SystemUiOverlayStyle.dark,
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: c.accent,
          foregroundColor: c.onAccent,
          disabledBackgroundColor: c.surfaceHigh,
          disabledForegroundColor: c.textFaint,
          minimumSize: const Size.fromHeight(Sizes.button),
          shape: rounded(Radii.md),
          textStyle: text.labelLarge,
          elevation: 0,
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: c.text,
          side: BorderSide(color: c.outline),
          minimumSize: const Size.fromHeight(Sizes.button),
          shape: rounded(Radii.md),
          textStyle: text.labelLarge,
        ),
      ),
      textButtonTheme: TextButtonThemeData(
        style: TextButton.styleFrom(
          foregroundColor: c.accentText,
          textStyle: text.labelLarge,
          minimumSize: const Size(Sizes.touchTarget, Sizes.touchTarget),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: c.surfaceRaised,
        contentPadding: const EdgeInsets.symmetric(horizontal: Space.lg, vertical: 18),
        border: inputBorder(Colors.transparent),
        enabledBorder: inputBorder(Colors.transparent),
        focusedBorder: inputBorder(c.accent, width: 1.5),
        errorBorder: inputBorder(c.danger),
        focusedErrorBorder: inputBorder(c.danger, width: 1.5),
        hintStyle: text.bodyLarge?.copyWith(color: c.textFaint),
        errorStyle: text.bodySmall?.copyWith(color: c.danger),
        errorMaxLines: 2,
      ),
      navigationBarTheme: NavigationBarThemeData(
        backgroundColor: c.surface,
        surfaceTintColor: Colors.transparent,
        indicatorColor: c.accent.withValues(alpha: 0.18),
        indicatorShape: rounded(Radii.pill),
        height: 68,
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        labelTextStyle: WidgetStateProperty.resolveWith((Set<WidgetState> s) {
          final bool selected = s.contains(WidgetState.selected);
          return text.labelSmall!.copyWith(
            color: selected ? c.text : c.textMuted,
            fontWeight: selected ? FontWeight.w600 : FontWeight.w500,
          );
        }),
        iconTheme: WidgetStateProperty.resolveWith((Set<WidgetState> s) {
          return IconThemeData(
            color: s.contains(WidgetState.selected) ? c.accentText : c.textMuted,
            size: Sizes.icon,
          );
        }),
      ),
      cardTheme: CardThemeData(
        color: c.surface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        margin: EdgeInsets.zero,
        shape: rounded(Radii.lg),
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: isDark ? Palette.ink3 : Palette.ink1,
        contentTextStyle: text.bodyMedium?.copyWith(color: Palette.textOnInk),
        actionTextColor: Palette.saffron,
        shape: rounded(Radii.md),
        insetPadding: const EdgeInsets.all(Space.lg),
      ),
      chipTheme: ChipThemeData(
        backgroundColor: c.surfaceHigh,
        selectedColor: c.accent.withValues(alpha: 0.18),
        labelStyle: text.labelMedium?.copyWith(color: c.text),
        side: BorderSide.none,
        shape: rounded(Radii.pill),
        padding: const EdgeInsets.symmetric(horizontal: Space.md, vertical: Space.sm),
      ),
      progressIndicatorTheme: ProgressIndicatorThemeData(
        color: c.accent,
        linearTrackColor: c.surfaceHigh,
        circularTrackColor: c.surfaceHigh,
      ),
      pageTransitionsTheme: const PageTransitionsTheme(
        builders: <TargetPlatform, PageTransitionsBuilder>{
          TargetPlatform.android: PredictiveBackPageTransitionsBuilder(),
          TargetPlatform.iOS: CupertinoPageTransitionsBuilder(),
        },
      ),
    );
  }
}
