import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/auth/auth_controller.dart';
import '../../../core/design/components/buttons.dart';
import '../../../core/design/theme/app_colors.dart';
import '../../../core/design/tokens/spacing.dart';
import '../../../core/l10n/l10n.dart';
import '../../../core/router/app_router.dart';
import 'language_chip.dart';

class WelcomeScreen extends ConsumerWidget {
  const WelcomeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final AppColors c = context.colors;
    final AppLocalizations l10n = context.l10n;
    final AuthState auth = ref.watch(authControllerProvider);
    final bool expired = auth is AuthSignedOut && auth.reason == SignOutReason.sessionExpired;

    return Scaffold(
      body: DecoratedBox(
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: <Color>[c.trainTint, c.bg],
            stops: const <double>[0, 0.6],
          ),
        ),
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: Space.gutter),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const Align(alignment: AlignmentDirectional.centerEnd, child: LanguageChip()),
                const Spacer(),
                _Mark(color: c.accent),
                const SizedBox(height: Space.xxl),
                Text(l10n.welcomeHeadline, style: context.text.displaySmall),
                const SizedBox(height: Space.lg),
                Text(
                  l10n.welcomeBody,
                  style: context.text.bodyLarge?.copyWith(color: c.textMuted),
                ),
                const Spacer(flex: 2),
                if (expired)
                  Padding(
                    padding: const EdgeInsets.only(bottom: Space.lg),
                    child: Text(
                      l10n.sessionExpired,
                      style: context.text.bodySmall?.copyWith(color: c.warning),
                    ),
                  ),
                PrimaryButton(label: l10n.signIn, onPressed: () => context.push(Routes.login)),
                const SizedBox(height: Space.md),
                SecondaryButton(
                  label: l10n.createAccount,
                  onPressed: () => context.push(Routes.register),
                ),
                const SizedBox(height: Space.lg),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Placeholder brand mark until the real identity lands: a saffron disc with
/// a bar, the shape a plate and a barbell share.
class _Mark extends StatelessWidget {
  const _Mark({required this.color});

  final Color color;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 56,
      height: 56,
      child: Stack(
        alignment: Alignment.center,
        children: <Widget>[
          DecoratedBox(
            decoration: BoxDecoration(shape: BoxShape.circle, color: color),
            child: const SizedBox.expand(),
          ),
          Container(
            width: 30,
            height: 6,
            decoration: BoxDecoration(
              color: context.colors.bg,
              borderRadius: BorderRadius.circular(3),
            ),
          ),
        ],
      ),
    );
  }
}
