import 'package:flutter/widgets.dart';

/// Pins a subtree left-to-right: numerals, phone numbers, timers, charts.
class Ltr extends StatelessWidget {
  const Ltr({super.key, required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Directionality(textDirection: TextDirection.ltr, child: child);
  }
}
