import 'package:flutter/material.dart';

import '../../core/l10n/l10n.dart';
import '../shell/placeholder_screen.dart';

class CommunityScreen extends StatelessWidget {
  const CommunityScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return PlaceholderScreen(title: context.l10n.tabCommunity, icon: Icons.people);
  }
}
