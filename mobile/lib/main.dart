import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app.dart';
import 'core/bootstrap.dart';

Future<void> main() async {
  final ProviderContainer container = await bootstrap();
  runApp(UncontrolledProviderScope(container: container, child: const App()));
}
