import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/auth/auth_controller.dart';
import '../../core/design/components/buttons.dart';
import '../../core/design/components/haptics.dart';
import '../../core/design/theme/app_colors.dart';
import '../../core/design/theme/theme_mode_controller.dart';
import '../../core/design/tokens/spacing.dart';
import '../../core/l10n/l10n.dart';
import '../../core/l10n/locale_controller.dart';

class MeScreen extends ConsumerWidget {
  const MeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final AppLocalizations l10n = context.l10n;
    final AppColors c = context.colors;
    final AuthState auth = ref.watch(authControllerProvider);
    final Locale locale = ref.watch(localeControllerProvider);
    final ThemeMode mode = ref.watch(themeModeControllerProvider);
    final String name = auth is AuthSignedIn ? auth.user.displayName : '';
    final String email = auth is AuthSignedIn ? auth.user.email : '';

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: Space.gutter, vertical: Space.lg),
          children: <Widget>[
            Text(l10n.tabMe, style: context.text.headlineLarge),
            const SizedBox(height: Space.xxl),
            Row(
              children: <Widget>[
                CircleAvatar(
                  radius: 28,
                  backgroundColor: c.accent,
                  child: Text(
                    name.isEmpty ? '?' : name.characters.first.toUpperCase(),
                    style: context.text.titleLarge?.copyWith(color: c.onAccent),
                  ),
                ),
                const SizedBox(width: Space.lg),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(name, style: context.text.titleLarge),
                      Text(
                        email,
                        style: context.text.bodySmall,
                        textDirection: TextDirection.ltr,
                        textAlign: TextAlign.start,
                      ),
                    ],
                  ),
                ),
              ],
            ),
            const SizedBox(height: Space.x3),
            _Setting(
              label: l10n.language,
              child: SegmentedButton<String>(
                segments: <ButtonSegment<String>>[
                  ButtonSegment<String>(value: 'en', label: Text(l10n.english)),
                  ButtonSegment<String>(value: 'ar', label: Text(l10n.arabic)),
                ],
                selected: <String>{locale.languageCode},
                showSelectedIcon: false,
                onSelectionChanged: (Set<String> s) {
                  Haptics.tick();
                  ref.read(localeControllerProvider.notifier).set(s.first);
                },
              ),
            ),
            const SizedBox(height: Space.xxl),
            _Setting(
              label: l10n.appearance,
              child: SegmentedButton<ThemeMode>(
                segments: <ButtonSegment<ThemeMode>>[
                  ButtonSegment<ThemeMode>(value: ThemeMode.system, label: Text(l10n.appearanceSystem)),
                  ButtonSegment<ThemeMode>(value: ThemeMode.light, label: Text(l10n.appearanceLight)),
                  ButtonSegment<ThemeMode>(value: ThemeMode.dark, label: Text(l10n.appearanceDark)),
                ],
                selected: <ThemeMode>{mode},
                showSelectedIcon: false,
                onSelectionChanged: (Set<ThemeMode> s) {
                  Haptics.tick();
                  ref.read(themeModeControllerProvider.notifier).set(s.first);
                },
              ),
            ),
            const SizedBox(height: Space.x4),
            SecondaryButton(
              label: l10n.signOut,
              icon: Icons.logout,
              onPressed: () => ref.read(authControllerProvider.notifier).signOut(),
            ),
          ],
        ),
      ),
    );
  }
}

class _Setting extends StatelessWidget {
  const _Setting({required this.label, required this.child});

  final String label;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(label, style: context.text.labelMedium),
        const SizedBox(height: Space.sm),
        SizedBox(width: double.infinity, child: child),
      ],
    );
  }
}
