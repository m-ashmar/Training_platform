import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/auth/auth_controller.dart';
import '../../../core/design/components/app_text_field.dart';
import '../../../core/design/components/buttons.dart';
import '../../../core/design/components/haptics.dart';
import '../../../core/design/theme/app_colors.dart';
import '../../../core/design/tokens/spacing.dart';
import '../../../core/l10n/error_messages.dart';
import '../../../core/l10n/l10n.dart';
import '../../../core/network/api_error.dart';
import '../../../core/router/app_router.dart';
import 'auth_scaffold.dart';
import 'validators.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final TextEditingController _email = TextEditingController();
  final TextEditingController _password = TextEditingController();
  String? _emailError;
  String? _passwordError;
  String? _formError;
  bool _busy = false;
  bool _showPassword = false;

  @override
  void dispose() {
    _email.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final AppLocalizations l10n = context.l10n;
    final String email = _email.text.trim();
    setState(() {
      _emailError = Validators.email(l10n, email);
      _passwordError = Validators.required(l10n, _password.text);
      _formError = null;
    });
    if (_emailError != null || _passwordError != null) return;

    setState(() => _busy = true);
    try {
      await ref.read(authControllerProvider.notifier).login(email: email, password: _password.text);
      // Success or "needs verification" both change auth state; the router
      // takes it from here.
    } on ApiError catch (e) {
      Haptics.error();
      if (!mounted) return;
      setState(() {
        _emailError = e.fieldMessage('email');
        _passwordError = e.fieldMessage('password');
        _formError = e.fieldErrors.isEmpty ? describeApiError(l10n, e) : e.fieldMessage('non_field_errors');
      });
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final AppLocalizations l10n = context.l10n;
    final AppColors c = context.colors;
    return AuthScaffold(
      title: l10n.signInTitle,
      lead: l10n.signInSubtitle,
      footer: PrimaryButton(label: l10n.continueLabel, loading: _busy, onPressed: _submit),
      children: <Widget>[
        AutofillGroup(
          child: Column(
            children: <Widget>[
              AppTextField(
                label: l10n.email,
                controller: _email,
                errorText: _emailError,
                keyboardType: TextInputType.emailAddress,
                textInputAction: TextInputAction.next,
                autofillHints: const <String>[AutofillHints.email],
                forceLtr: true,
                autofocus: true,
                enabled: !_busy,
              ),
              const SizedBox(height: Space.xl),
              AppTextField(
                label: l10n.password,
                controller: _password,
                errorText: _passwordError,
                obscureText: !_showPassword,
                textInputAction: TextInputAction.done,
                autofillHints: const <String>[AutofillHints.password],
                forceLtr: true,
                enabled: !_busy,
                onSubmitted: (_) => _submit(),
                suffix: IconButton(
                  icon: Icon(_showPassword ? Icons.visibility_off_outlined : Icons.visibility_outlined),
                  color: c.textMuted,
                  onPressed: () => setState(() => _showPassword = !_showPassword),
                ),
              ),
            ],
          ),
        ),
        if (_formError != null) ...<Widget>[
          const SizedBox(height: Space.lg),
          Text(_formError!, style: context.text.bodyMedium?.copyWith(color: c.danger)),
        ],
        const SizedBox(height: Space.xxl),
        Align(
          alignment: AlignmentDirectional.centerStart,
          child: TextButton(
            onPressed: _busy ? null : () => context.pushReplacement(Routes.register),
            child: Text(l10n.createAccount),
          ),
        ),
      ],
    );
  }
}
