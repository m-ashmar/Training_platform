import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/auth/auth_controller.dart';
import '../../../core/design/components/buttons.dart';
import '../../../core/design/components/haptics.dart';
import '../../../core/design/components/otp_code_field.dart';
import '../../../core/design/theme/app_colors.dart';
import '../../../core/design/tokens/spacing.dart';
import '../../../core/l10n/error_messages.dart';
import '../../../core/l10n/l10n.dart';
import '../../../core/network/api_error.dart';
import 'auth_scaffold.dart';

class OtpScreen extends ConsumerStatefulWidget {
  const OtpScreen({super.key});

  @override
  ConsumerState<OtpScreen> createState() => _OtpScreenState();
}

class _OtpScreenState extends ConsumerState<OtpScreen> {
  static const int _resendCooldownSeconds = 60;

  final GlobalKey<OtpCodeFieldState> _field = GlobalKey<OtpCodeFieldState>();
  String _code = '';
  String? _error;
  bool _busy = false;
  int _cooldown = _resendCooldownSeconds;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _startCooldown();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  void _startCooldown([int seconds = _resendCooldownSeconds]) {
    _timer?.cancel();
    setState(() => _cooldown = seconds);
    _timer = Timer.periodic(const Duration(seconds: 1), (Timer t) {
      if (_cooldown <= 1) {
        t.cancel();
        setState(() => _cooldown = 0);
      } else {
        setState(() => _cooldown--);
      }
    });
  }

  Future<void> _verify(String code) async {
    if (_busy || code.length != 6) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(authControllerProvider.notifier).verifyOtp(code);
      Haptics.success();
    } on ApiError catch (e) {
      Haptics.error();
      if (!mounted) return;
      final AppLocalizations l10n = context.l10n;
      setState(() {
        // The server deliberately answers every rejected code the same way.
        _error = e.status == 400 || e.code == ApiCode.validationError
            ? l10n.codeRejected
            : describeApiError(l10n, e);
      });
      _field.currentState?.clear();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _resend() async {
    final AppLocalizations l10n = context.l10n;
    final ScaffoldMessengerState messenger = ScaffoldMessenger.of(context);
    try {
      await ref.read(authControllerProvider.notifier).resendOtp();
      Haptics.tick();
      messenger.showSnackBar(SnackBar(content: Text(l10n.codeSent)));
      _startCooldown();
    } on ApiError catch (e) {
      Haptics.error();
      messenger.showSnackBar(SnackBar(content: Text(describeApiError(l10n, e))));
      if (e.code == ApiCode.rateLimited) _startCooldown(e.retryAfter?.inSeconds ?? 300);
    }
  }

  @override
  Widget build(BuildContext context) {
    final AppLocalizations l10n = context.l10n;
    final AppColors c = context.colors;
    final AuthState auth = ref.watch(authControllerProvider);
    final String email = auth is AuthUnverified ? auth.email : '';

    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (bool didPop, _) {
        if (!didPop) ref.read(authControllerProvider.notifier).leaveVerification();
      },
      child: AuthScaffold(
        title: l10n.verifyTitle,
        lead: l10n.verifySubtitle(email),
        footer: PrimaryButton(
          label: l10n.verifyAction,
          loading: _busy,
          onPressed: _code.length == 6 ? () => _verify(_code) : null,
        ),
        children: <Widget>[
          OtpCodeField(
            key: _field,
            hasError: _error != null,
            enabled: !_busy,
            onChanged: (String v) => setState(() {
              _code = v;
              if (_error != null) _error = null;
            }),
            onCompleted: _verify,
          ),
          const SizedBox(height: Space.lg),
          AnimatedSwitcher(
            duration: Motion.base,
            child: _error == null
                ? const SizedBox(height: 20)
                : Text(
                    _error!,
                    key: ValueKey<String>(_error!),
                    style: context.text.bodyMedium?.copyWith(color: c.danger),
                  ),
          ),
          const SizedBox(height: Space.xxl),
          Align(
            alignment: AlignmentDirectional.centerStart,
            child: TextButton(
              onPressed: _cooldown == 0 && !_busy ? _resend : null,
              child: Text(_cooldown == 0 ? l10n.resendCode : l10n.resendIn(_cooldown)),
            ),
          ),
        ],
      ),
    );
  }
}
