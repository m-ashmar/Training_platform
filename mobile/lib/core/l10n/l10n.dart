import 'package:flutter/widgets.dart';
import 'package:training_app/l10n/app_localizations.dart';

export 'package:training_app/l10n/app_localizations.dart';

extension L10nContext on BuildContext {
  AppLocalizations get l10n => AppLocalizations.of(this);

  bool get isRtl => Directionality.of(this) == TextDirection.rtl;
}
