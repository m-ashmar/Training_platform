import 'package:flutter/material.dart';

import '../../core/l10n/l10n.dart';
import '../shell/placeholder_screen.dart';

class EatScreen extends StatelessWidget {
  const EatScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return PlaceholderScreen(title: context.l10n.tabEat, icon: Icons.restaurant);
  }
}
