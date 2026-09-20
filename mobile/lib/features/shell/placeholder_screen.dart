import 'package:flutter/material.dart';

import '../../core/design/theme/app_colors.dart';
import '../../core/design/tokens/spacing.dart';
import '../../core/design/tokens/typography.dart';
import '../../core/l10n/l10n.dart';

/// Honest placeholder for a tab that has no feature yet: title, one line,
/// nothing that pretends to work.
class PlaceholderScreen extends StatelessWidget {
  const PlaceholderScreen({super.key, required this.title, required this.icon});

  final String title;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final AppColors c = context.colors;
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: Space.gutter, vertical: Space.lg),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(title, style: context.text.headlineLarge),
              const Spacer(),
              Center(
                child: Column(
                  children: <Widget>[
                    Icon(icon, size: 40, color: c.textFaint),
                    const SizedBox(height: Space.md),
                    Text(
                      context.l10n.comingSoon.toUpperCase(),
                      style: context.text.eyebrow.copyWith(color: c.textFaint),
                    ),
                  ],
                ),
              ),
              const Spacer(flex: 2),
            ],
          ),
        ),
      ),
    );
  }
}
