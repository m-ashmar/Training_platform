import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/design/components/haptics.dart';
import '../../../core/design/theme/app_colors.dart';
import '../../../core/design/tokens/spacing.dart';
import '../../../core/l10n/locale_controller.dart';

/// One-tap language flip for screens the user reaches before signing in.
class LanguageChip extends ConsumerWidget {
  const LanguageChip({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final bool arabic = ref.watch(localeControllerProvider).languageCode == 'ar';
    final AppColors c = context.colors;
    return Padding(
      padding: const EdgeInsetsDirectional.only(end: Space.sm),
      child: ActionChip(
        avatar: Icon(Icons.language, size: 18, color: c.textMuted),
        label: Text(arabic ? 'English' : 'العربية'),
        onPressed: () {
          Haptics.tick();
          ref.read(localeControllerProvider.notifier).set(arabic ? 'en' : 'ar');
        },
      ),
    );
  }
}
