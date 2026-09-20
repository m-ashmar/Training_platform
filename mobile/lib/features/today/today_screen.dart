import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../../core/auth/auth_controller.dart';
import '../../core/design/theme/app_colors.dart';
import '../../core/design/tokens/spacing.dart';
import '../../core/design/tokens/typography.dart';
import '../../core/l10n/l10n.dart';

/// The home: one day, one scroll. Time-aware greeting now; the session and
/// plate blocks fill in as the Train and Eat features land.
class TodayScreen extends ConsumerWidget {
  const TodayScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final AppLocalizations l10n = context.l10n;
    final AppColors c = context.colors;
    final AuthState auth = ref.watch(authControllerProvider);
    final String name = auth is AuthSignedIn ? auth.user.displayName : '';
    final DateTime now = DateTime.now();
    final String greeting = switch (now.hour) {
      < 12 => l10n.greetingMorning(name),
      < 17 => l10n.greetingAfternoon(name),
      _ => l10n.greetingEvening(name),
    };
    final String date = DateFormat.MMMMEEEEd(Localizations.localeOf(context).toString()).format(now);

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.symmetric(horizontal: Space.gutter, vertical: Space.lg),
          children: <Widget>[
            Text(date.toUpperCase(), style: context.text.eyebrow.copyWith(color: c.textMuted)),
            const SizedBox(height: Space.sm),
            Text(greeting, style: context.text.headlineLarge),
            const SizedBox(height: Space.x3),
            _Block(
              eyebrow: l10n.tabTrain,
              title: l10n.todayWorkoutTitle,
              tint: c.trainTint,
              icon: Icons.fitness_center,
            ),
            const SizedBox(height: Space.lg),
            _Block(
              eyebrow: l10n.tabEat,
              title: l10n.todayMealsTitle,
              tint: c.eatTint,
              icon: Icons.restaurant,
            ),
          ],
        ),
      ),
    );
  }
}

class _Block extends StatelessWidget {
  const _Block({
    required this.eyebrow,
    required this.title,
    required this.tint,
    required this.icon,
  });

  final String eyebrow;
  final String title;
  final Color tint;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final AppColors c = context.colors;
    return Card(
      color: tint,
      child: Padding(
        padding: const EdgeInsets.all(Space.xl),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Icon(icon, size: 18, color: c.accentText),
                const SizedBox(width: Space.sm),
                Text(eyebrow.toUpperCase(), style: context.text.eyebrow.copyWith(color: c.accentText)),
              ],
            ),
            const SizedBox(height: Space.md),
            Text(title, style: context.text.titleLarge),
            const SizedBox(height: Space.sm),
            Text(
              context.l10n.comingSoon,
              style: context.text.bodyMedium?.copyWith(color: c.textMuted),
            ),
          ],
        ),
      ),
    );
  }
}
