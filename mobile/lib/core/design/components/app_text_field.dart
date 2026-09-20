import 'package:flutter/material.dart';

import '../theme/app_colors.dart';
import '../tokens/spacing.dart';

/// Labelled input. Label sits above the field so RTL and LTR share one
/// layout; [forceLtr] keeps emails, phones and passwords left-to-right even
/// inside an Arabic screen.
class AppTextField extends StatelessWidget {
  const AppTextField({
    super.key,
    required this.label,
    this.controller,
    this.hint,
    this.errorText,
    this.keyboardType,
    this.obscureText = false,
    this.textInputAction,
    this.autofillHints,
    this.onSubmitted,
    this.onChanged,
    this.forceLtr = false,
    this.autofocus = false,
    this.enabled = true,
    this.suffix,
    this.textCapitalization = TextCapitalization.none,
  });

  final String label;
  final TextEditingController? controller;
  final String? hint;
  final String? errorText;
  final TextInputType? keyboardType;
  final bool obscureText;
  final TextInputAction? textInputAction;
  final Iterable<String>? autofillHints;
  final ValueChanged<String>? onSubmitted;
  final ValueChanged<String>? onChanged;
  final bool forceLtr;
  final bool autofocus;
  final bool enabled;
  final Widget? suffix;
  final TextCapitalization textCapitalization;

  @override
  Widget build(BuildContext context) {
    final AppColors c = context.colors;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(label, style: context.text.labelMedium?.copyWith(color: c.textMuted)),
        const SizedBox(height: Space.sm),
        TextField(
          controller: controller,
          enabled: enabled,
          autofocus: autofocus,
          keyboardType: keyboardType,
          obscureText: obscureText,
          textInputAction: textInputAction,
          autofillHints: autofillHints,
          onSubmitted: onSubmitted,
          onChanged: onChanged,
          textCapitalization: textCapitalization,
          textDirection: forceLtr ? TextDirection.ltr : null,
          textAlign: forceLtr ? TextAlign.left : TextAlign.start,
          style: context.text.bodyLarge,
          cursorColor: c.accent,
          decoration: InputDecoration(
            hintText: hint,
            errorText: errorText,
            suffixIcon: suffix,
          ),
        ),
      ],
    );
  }
}
