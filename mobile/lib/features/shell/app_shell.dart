import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/design/components/haptics.dart';
import '../../core/design/theme/app_colors.dart';
import '../../core/design/tokens/spacing.dart';
import '../../core/l10n/l10n.dart';

/// Bottom navigation for the client app. The page background shifts
/// temperature with the tab: cool for Train, warm for Eat, neutral elsewhere.
class AppShell extends StatelessWidget {
  const AppShell({super.key, required this.shell});

  final StatefulNavigationShell shell;

  static const int trainIndex = 1;
  static const int eatIndex = 2;

  @override
  Widget build(BuildContext context) {
    final AppLocalizations l10n = context.l10n;
    final AppColors c = context.colors;
    final Color wash = switch (shell.currentIndex) {
      trainIndex => c.trainTint,
      eatIndex => c.eatTint,
      _ => c.bg,
    };

    return AnimatedContainer(
      duration: Motion.deliberate,
      curve: Motion.emphasized,
      color: wash,
      child: Scaffold(
        backgroundColor: Colors.transparent,
        body: shell,
        bottomNavigationBar: NavigationBar(
          selectedIndex: shell.currentIndex,
          onDestinationSelected: (int i) {
            Haptics.tick();
            shell.goBranch(i, initialLocation: i == shell.currentIndex);
          },
          destinations: <NavigationDestination>[
            NavigationDestination(
              icon: const Icon(Icons.wb_sunny_outlined),
              selectedIcon: const Icon(Icons.wb_sunny),
              label: l10n.tabToday,
            ),
            NavigationDestination(
              icon: const Icon(Icons.fitness_center_outlined),
              selectedIcon: const Icon(Icons.fitness_center),
              label: l10n.tabTrain,
            ),
            NavigationDestination(
              icon: const Icon(Icons.restaurant_outlined),
              selectedIcon: const Icon(Icons.restaurant),
              label: l10n.tabEat,
            ),
            NavigationDestination(
              icon: const Icon(Icons.people_outline),
              selectedIcon: const Icon(Icons.people),
              label: l10n.tabCommunity,
            ),
            NavigationDestination(
              icon: const Icon(Icons.person_outline),
              selectedIcon: const Icon(Icons.person),
              label: l10n.tabMe,
            ),
          ],
        ),
      ),
    );
  }
}
