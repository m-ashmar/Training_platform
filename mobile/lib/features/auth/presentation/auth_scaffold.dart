import 'package:flutter/material.dart';

import '../../../core/design/theme/app_colors.dart';
import '../../../core/design/tokens/spacing.dart';

/// Shared frame for the sign-in flow: back button, big title, short lead,
/// content, and a footer pinned above the keyboard.
class AuthScaffold extends StatelessWidget {
  const AuthScaffold({
    super.key,
    required this.title,
    required this.lead,
    required this.children,
    this.footer,
    this.actions,
  });

  final String title;
  final String lead;
  final List<Widget> children;
  final Widget? footer;
  final List<Widget>? actions;

  @override
  Widget build(BuildContext context) {
    final AppColors c = context.colors;
    return Scaffold(
      appBar: AppBar(actions: actions),
      body: SafeArea(
        top: false,
        child: Column(
          children: <Widget>[
            Expanded(
              child: SingleChildScrollView(
                keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
                padding: const EdgeInsets.fromLTRB(Space.gutter, Space.sm, Space.gutter, Space.xxl),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Text(title, style: context.text.headlineLarge),
                    const SizedBox(height: Space.sm),
                    Text(lead, style: context.text.bodyLarge?.copyWith(color: c.textMuted)),
                    const SizedBox(height: Space.x3),
                    ...children,
                  ],
                ),
              ),
            ),
            if (footer != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(Space.gutter, 0, Space.gutter, Space.lg),
                child: footer,
              ),
          ],
        ),
      ),
    );
  }
}
