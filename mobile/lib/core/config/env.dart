/// Build-time environment. Set with `--dart-define`, never edited in code.
///
/// ```sh
/// flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000 --dart-define=FLAVOR=dev
/// ```
///
/// Android emulator reaches the host machine at `http://10.0.2.2:8000`;
/// the iOS simulator uses `http://127.0.0.1:8000`.
enum Flavor { dev, staging, prod }

abstract final class Env {
  static const Flavor flavor = String.fromEnvironment('FLAVOR', defaultValue: 'dev') == 'prod'
      ? Flavor.prod
      : String.fromEnvironment('FLAVOR', defaultValue: 'dev') == 'staging'
          ? Flavor.staging
          : Flavor.dev;

  /// Origin only, no trailing slash. All paths in the app start with `/api/`.
  static const String apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://127.0.0.1:8000',
  );

  static bool get isProd => flavor == Flavor.prod;
}
