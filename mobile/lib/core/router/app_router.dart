import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../features/auth/presentation/login_screen.dart';
import '../../features/auth/presentation/otp_screen.dart';
import '../../features/auth/presentation/register_screen.dart';
import '../../features/auth/presentation/splash_screen.dart';
import '../../features/auth/presentation/suspended_screen.dart';
import '../../features/auth/presentation/welcome_screen.dart';
import '../../features/community/community_screen.dart';
import '../../features/eat/eat_screen.dart';
import '../../features/me/me_screen.dart';
import '../../features/shell/app_shell.dart';
import '../../features/today/today_screen.dart';
import '../../features/train/train_screen.dart';
import '../auth/auth_controller.dart';

abstract final class Routes {
  static const String splash = '/';
  static const String welcome = '/welcome';
  static const String login = '/login';
  static const String register = '/register';
  static const String verify = '/verify';
  static const String suspended = '/suspended';
  static const String today = '/today';
  static const String train = '/train';
  static const String eat = '/eat';
  static const String community = '/community';
  static const String me = '/me';

  static const Set<String> authFlow = <String>{welcome, login, register};
}

/// Auth state decides where you can be; screens never navigate "to login",
/// they change state and the redirect follows.
final routerProvider = Provider<GoRouter>((Ref ref) {
  final _AuthRefresh refresh = _AuthRefresh(ref);
  ref.onDispose(refresh.dispose);

  return GoRouter(
    initialLocation: Routes.today,
    refreshListenable: refresh,
    debugLogDiagnostics: kDebugMode,
    redirect: (BuildContext context, GoRouterState state) {
      final AuthState auth = ref.read(authControllerProvider);
      final String at = state.matchedLocation;
      return switch (auth) {
        AuthUnknown() => at == Routes.splash ? null : Routes.splash,
        AuthSignedOut() => Routes.authFlow.contains(at) ? null : Routes.welcome,
        AuthUnverified() => at == Routes.verify ? null : Routes.verify,
        AuthSuspended() => at == Routes.suspended ? null : Routes.suspended,
        AuthSignedIn() => Routes.authFlow.contains(at) ||
                at == Routes.splash ||
                at == Routes.verify ||
                at == Routes.suspended
            ? Routes.today
            : null,
      };
    },
    routes: <RouteBase>[
      GoRoute(path: Routes.splash, builder: (_, _) => const SplashScreen()),
      GoRoute(
        path: Routes.welcome,
        pageBuilder: (_, GoRouterState s) => _fade(s, const WelcomeScreen()),
      ),
      GoRoute(path: Routes.login, builder: (_, _) => const LoginScreen()),
      GoRoute(path: Routes.register, builder: (_, _) => const RegisterScreen()),
      GoRoute(
        path: Routes.verify,
        pageBuilder: (_, GoRouterState s) => _fade(s, const OtpScreen()),
      ),
      GoRoute(
        path: Routes.suspended,
        pageBuilder: (_, GoRouterState s) => _fade(s, const SuspendedScreen()),
      ),
      StatefulShellRoute.indexedStack(
        builder: (BuildContext context, GoRouterState state, StatefulNavigationShell shell) =>
            AppShell(shell: shell),
        branches: <StatefulShellBranch>[
          StatefulShellBranch(
            routes: <RouteBase>[
              GoRoute(path: Routes.today, builder: (_, _) => const TodayScreen()),
            ],
          ),
          StatefulShellBranch(
            routes: <RouteBase>[
              GoRoute(path: Routes.train, builder: (_, _) => const TrainScreen()),
            ],
          ),
          StatefulShellBranch(
            routes: <RouteBase>[
              GoRoute(path: Routes.eat, builder: (_, _) => const EatScreen()),
            ],
          ),
          StatefulShellBranch(
            routes: <RouteBase>[
              GoRoute(path: Routes.community, builder: (_, _) => const CommunityScreen()),
            ],
          ),
          StatefulShellBranch(
            routes: <RouteBase>[
              GoRoute(path: Routes.me, builder: (_, _) => const MeScreen()),
            ],
          ),
        ],
      ),
    ],
  );
});

CustomTransitionPage<void> _fade(GoRouterState state, Widget child) {
  return CustomTransitionPage<void>(
    key: state.pageKey,
    child: child,
    transitionDuration: const Duration(milliseconds: 320),
    transitionsBuilder: (_, Animation<double> a, _, Widget child) =>
        FadeTransition(opacity: CurveTween(curve: Curves.easeOutCubic).animate(a), child: child),
  );
}

class _AuthRefresh extends ChangeNotifier {
  _AuthRefresh(Ref ref) {
    ref.listen<AuthState>(authControllerProvider, (_, _) => notifyListeners());
  }
}
