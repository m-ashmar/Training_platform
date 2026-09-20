import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/auth/app_user.dart';
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

class RegisterScreen extends ConsumerStatefulWidget {
  const RegisterScreen({super.key});

  @override
  ConsumerState<RegisterScreen> createState() => _RegisterScreenState();
}

class _RegisterScreenState extends ConsumerState<RegisterScreen> {
  final TextEditingController _username = TextEditingController();
  final TextEditingController _email = TextEditingController();
  final TextEditingController _phone = TextEditingController();
  final TextEditingController _password = TextEditingController();
  final Map<String, String?> _errors = <String, String?>{};
  UserRole _role = UserRole.client;
  String? _formError;
  bool _busy = false;

  @override
  void dispose() {
    _username.dispose();
    _email.dispose();
    _phone.dispose();
    _password.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final AppLocalizations l10n = context.l10n;
    setState(() {
      _errors
        ..['username'] = Validators.required(l10n, _username.text)
        ..['email'] = Validators.email(l10n, _email.text)
        ..['phone_number'] = Validators.required(l10n, _phone.text)
        ..['password1'] = Validators.password(l10n, _password.text);
      _formError = null;
    });
    if (_errors.values.any((String? e) => e != null)) return;

    setState(() => _busy = true);
    try {
      await ref.read(authControllerProvider.notifier).register(
            username: _username.text.trim(),
            email: _email.text.trim(),
            password: _password.text,
            phoneNumber: _phone.text.trim(),
            role: _role,
          );
    } on ApiError catch (e) {
      Haptics.error();
      if (!mounted) return;
      setState(() {
        for (final String field in <String>['username', 'email', 'phone_number', 'password1']) {
          _errors[field] = e.fieldMessage(field);
        }
        _formError = e.fieldErrors.isEmpty
            ? describeApiError(l10n, e)
            : e.fieldMessage('non_field_errors') ?? e.fieldMessage('password2');
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
      title: l10n.registerTitle,
      lead: l10n.registerSubtitle,
      footer: PrimaryButton(label: l10n.createAccount, loading: _busy, onPressed: _submit),
      children: <Widget>[
        SegmentedButton<UserRole>(
          segments: <ButtonSegment<UserRole>>[
            ButtonSegment<UserRole>(
              value: UserRole.client,
              label: Text(l10n.iAmClient),
              icon: const Icon(Icons.fitness_center),
            ),
            ButtonSegment<UserRole>(
              value: UserRole.trainer,
              label: Text(l10n.iAmTrainer),
              icon: const Icon(Icons.sports),
            ),
          ],
          selected: <UserRole>{_role},
          showSelectedIcon: false,
          onSelectionChanged: _busy
              ? null
              : (Set<UserRole> s) {
                  Haptics.tick();
                  setState(() => _role = s.first);
                },
        ),
        const SizedBox(height: Space.xxl),
        AutofillGroup(
          child: Column(
            children: <Widget>[
              AppTextField(
                label: l10n.username,
                controller: _username,
                errorText: _errors['username'],
                textInputAction: TextInputAction.next,
                autofillHints: const <String>[AutofillHints.newUsername],
                forceLtr: true,
                enabled: !_busy,
              ),
              const SizedBox(height: Space.xl),
              AppTextField(
                label: l10n.email,
                controller: _email,
                errorText: _errors['email'],
                keyboardType: TextInputType.emailAddress,
                textInputAction: TextInputAction.next,
                autofillHints: const <String>[AutofillHints.email],
                forceLtr: true,
                enabled: !_busy,
              ),
              const SizedBox(height: Space.xl),
              AppTextField(
                label: l10n.phoneNumber,
                controller: _phone,
                errorText: _errors['phone_number'],
                keyboardType: TextInputType.phone,
                textInputAction: TextInputAction.next,
                autofillHints: const <String>[AutofillHints.telephoneNumber],
                forceLtr: true,
                enabled: !_busy,
              ),
              const SizedBox(height: Space.xl),
              AppTextField(
                label: l10n.password,
                controller: _password,
                errorText: _errors['password1'],
                obscureText: true,
                textInputAction: TextInputAction.done,
                autofillHints: const <String>[AutofillHints.newPassword],
                forceLtr: true,
                enabled: !_busy,
                onSubmitted: (_) => _submit(),
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
            onPressed: _busy ? null : () => context.pushReplacement(Routes.login),
            child: Text(l10n.signIn),
          ),
        ),
      ],
    );
  }
}
