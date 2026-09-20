import 'package:flutter/material.dart';

import '../../core/l10n/l10n.dart';
import '../shell/placeholder_screen.dart';

class TrainScreen extends StatelessWidget {
  const TrainScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return PlaceholderScreen(title: context.l10n.tabTrain, icon: Icons.fitness_center);
  }
}
