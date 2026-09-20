import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/auth/auth_controller.dart';
import '../../../core/design/components/buttons.dart';
import '../../../core/design/theme/app_colors.dart';
import '../../../core/design/tokens/spacing.dart';
import '../../../core/l10n/l10n.dart';

class SuspendedScreen extends ConsumerWidget {
  const SuspendedScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final AppLocalizations l10n = context.l10n;
    final AppColors c = context.colors;
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: Space.gutter),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              const Spacer(),
              Icon(Icons.lock_outline, size: 40, color: c.warning),
              const SizedBox(height: Space.xxl),
              Text(l10n.suspendedTitle, style: context.text.headlineMedium),
              const SizedBox(height: Space.md),
              Text(l10n.suspendedBody, style: context.text.bodyLarge?.copyWith(color: c.textMuted)),
              const Spacer(flex: 2),
              SecondaryButton(
                label: l10n.back,
                onPressed: () => ref.read(authControllerProvider.notifier).leaveVerification(),
              ),
              const SizedBox(height: Space.lg),
            ],
          ),
        ),
      ),
    );
  }
}
