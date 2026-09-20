import 'package:flutter/material.dart';

import '../theme/app_colors.dart';
import '../tokens/spacing.dart';
import 'haptics.dart';

/// The one filled call to action on a screen. Shows progress in place while
/// [loading] and keeps its colour, so the layout never jumps.
class PrimaryButton extends StatelessWidget {
  const PrimaryButton({
    super.key,
    required this.label,
    this.onPressed,
    this.loading = false,
    this.icon,
  });

  final String label;
  final VoidCallback? onPressed;
  final bool loading;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final AppColors c = context.colors;
    final bool enabled = onPressed != null;
    return FilledButton(
      onPressed: enabled
          ? () {
              if (loading) return;
              Haptics.tap();
              onPressed!();
            }
          : null,
      child: AnimatedSwitcher(
        duration: Motion.base,
        switchInCurve: Motion.standard,
        switchOutCurve: Motion.standard,
        child: loading
            ? SizedBox(
                key: const ValueKey<String>('progress'),
                width: 22,
                height: 22,
                child: CircularProgressIndicator(strokeWidth: 2.4, color: c.onAccent),
              )
            : Row(
                key: const ValueKey<String>('label'),
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  if (icon != null) ...<Widget>[
                    Icon(icon, size: 20),
                    const SizedBox(width: Space.sm),
                  ],
                  Text(label),
                ],
              ),
      ),
    );
  }
}

/// Outlined companion to [PrimaryButton].
class SecondaryButton extends StatelessWidget {
  const SecondaryButton({super.key, required this.label, this.onPressed, this.icon});

  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return OutlinedButton(
      onPressed: onPressed == null
          ? null
          : () {
              Haptics.tap();
              onPressed!();
            },
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: <Widget>[
          if (icon != null) ...<Widget>[
            Icon(icon, size: 20),
            const SizedBox(width: Space.sm),
          ],
          Text(label),
        ],
      ),
    );
  }
}
