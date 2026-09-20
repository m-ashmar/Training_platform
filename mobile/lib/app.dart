import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/design/theme/app_theme.dart';
import 'core/design/theme/theme_mode_controller.dart';
import 'core/design/tokens/spacing.dart';
import 'core/l10n/l10n.dart';
import 'core/l10n/locale_controller.dart';
import 'core/router/app_router.dart';

class App extends ConsumerWidget {
  const App({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final Locale locale = ref.watch(localeControllerProvider);
    final ThemeMode mode = ref.watch(themeModeControllerProvider);
    final bool arabic = locale.languageCode == 'ar';

    return MaterialApp.router(
      onGenerateTitle: (BuildContext context) => context.l10n.appName,
      routerConfig: ref.watch(routerProvider),
      locale: locale,
      supportedLocales: AppLocalizations.supportedLocales,
      localizationsDelegates: AppLocalizations.localizationsDelegates,
      theme: AppTheme.light(arabic: arabic),
      darkTheme: AppTheme.dark(arabic: arabic),
      themeMode: mode,
      themeAnimationDuration: Motion.slow,
      themeAnimationCurve: Motion.standard,
      debugShowCheckedModeBanner: false,
    );
  }
}
