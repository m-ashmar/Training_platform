import '../network/api_error.dart';
import 'l10n.dart';

/// Local copy for every code that has a fixed meaning; the server's
/// translated `detail` only fills in for codes the app has no line for.
String describeApiError(AppLocalizations l10n, ApiError e) {
  return switch (e.code) {
    ApiCode.network => l10n.errorNetwork,
    ApiCode.timeout => l10n.errorTimeout,
    ApiCode.serverError => l10n.errorServer,
    ApiCode.rateLimited => l10n.errorRateLimited(e.retryAfter?.inSeconds ?? 60),
    ApiCode.notAuthenticated || ApiCode.tokenNotValid => l10n.sessionExpired,
    ApiCode.noEntitlement => l10n.renewBody,
    ApiCode.featureNotInPlan => l10n.notInPlanBody,
    ApiCode.accountDisabled => l10n.suspendedBody,
    _ => e.message.isNotEmpty ? e.message : l10n.errorUnknown,
  };
}
