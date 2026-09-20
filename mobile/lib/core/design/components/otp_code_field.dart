import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../theme/app_colors.dart';
import '../tokens/spacing.dart';
import '../tokens/typography.dart';

/// Six boxes, one invisible text field. Always LTR (codes are read left to
/// right in Arabic too), accepts paste and system one-time-code autofill.
class OtpCodeField extends StatefulWidget {
  const OtpCodeField({
    super.key,
    this.length = 6,
    required this.onCompleted,
    this.onChanged,
    this.hasError = false,
    this.enabled = true,
    this.autofocus = true,
  });

  final int length;
  final ValueChanged<String> onCompleted;
  final ValueChanged<String>? onChanged;
  final bool hasError;
  final bool enabled;
  final bool autofocus;

  @override
  State<OtpCodeField> createState() => OtpCodeFieldState();
}

class OtpCodeFieldState extends State<OtpCodeField> {
  final TextEditingController _controller = TextEditingController();
  final FocusNode _focus = FocusNode();

  String get value => _controller.text;

  void clear() {
    _controller.clear();
    _focus.requestFocus();
    setState(() {});
  }

  @override
  void initState() {
    super.initState();
    _controller.addListener(_onText);
    _focus.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _controller.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _onText() {
    setState(() {});
    widget.onChanged?.call(_controller.text);
    if (_controller.text.length == widget.length) {
      widget.onCompleted(_controller.text);
    }
  }

  @override
  Widget build(BuildContext context) {
    final AppColors c = context.colors;
    final String text = _controller.text;
    final int active = text.length.clamp(0, widget.length - 1);

    return Directionality(
      textDirection: TextDirection.ltr,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTap: widget.enabled ? _focus.requestFocus : null,
        child: Stack(
          alignment: Alignment.center,
          children: <Widget>[
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: List<Widget>.generate(widget.length, (int i) {
                final bool filled = i < text.length;
                final bool isActive = _focus.hasFocus && i == active && !filled ||
                    _focus.hasFocus && text.length == widget.length && i == widget.length - 1;
                final Color border = widget.hasError
                    ? c.danger
                    : isActive
                        ? c.accent
                        : filled
                            ? c.outline
                            : Colors.transparent;
                return AnimatedContainer(
                  duration: Motion.fast,
                  curve: Motion.standard,
                  width: 48,
                  height: Sizes.field,
                  alignment: Alignment.center,
                  decoration: BoxDecoration(
                    color: c.surfaceRaised,
                    borderRadius: BorderRadius.circular(Radii.md),
                    border: Border.all(color: border, width: isActive ? 1.5 : 1),
                  ),
                  child: Text(
                    filled ? text[i] : '',
                    style: context.text.headlineSmall?.copyWith(
                      fontFeatures: kTabularFigures,
                      color: widget.hasError ? c.danger : c.text,
                    ),
                  ),
                );
              }),
            ),
            // The real input: invisible, full width, so the keyboard and
            // autofill work while the boxes above do the drawing.
            Positioned.fill(
              child: Opacity(
                opacity: 0,
                child: TextField(
                  controller: _controller,
                  focusNode: _focus,
                  enabled: widget.enabled,
                  autofocus: widget.autofocus,
                  keyboardType: TextInputType.number,
                  textInputAction: TextInputAction.done,
                  autofillHints: const <String>[AutofillHints.oneTimeCode],
                  enableInteractiveSelection: false,
                  showCursor: false,
                  inputFormatters: <TextInputFormatter>[
                    FilteringTextInputFormatter.digitsOnly,
                    LengthLimitingTextInputFormatter(widget.length),
                  ],
                  decoration: const InputDecoration.collapsed(hintText: ''),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
