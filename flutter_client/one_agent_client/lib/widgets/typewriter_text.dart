import 'dart:async';

import 'package:flutter/material.dart';

class TypewriterText extends StatefulWidget {
  final String text;
  final TextStyle? style;
  final TextAlign? textAlign;
  final Duration speed;

  const TypewriterText(
    this.text, {
    super.key,
    this.style,
    this.textAlign,
    this.speed = const Duration(milliseconds: 20),
  });

  @override
  State<TypewriterText> createState() => _TypewriterTextState();
}

class _TypewriterTextState extends State<TypewriterText> {
  String _displayText = '';
  int _lastLength = 0;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _animateTo(widget.text);
  }

  @override
  void didUpdateWidget(TypewriterText oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.text != oldWidget.text) {
      _animateTo(widget.text);
    }
  }

  void _animateTo(String target) {
    _timer?.cancel();
    final startIdx = _displayText.length;
    if (target.length <= startIdx) {
      _displayText = target;
      _lastLength = target.length;
      return;
    }
    _lastLength = target.length;
    _timer = Timer.periodic(widget.speed, (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      final current = _displayText.length;
      if (current >= target.length) {
        timer.cancel();
        return;
      }
      final nextEnd = (current + 2).clamp(0, target.length);
      setState(() {
        _displayText = target.substring(0, nextEnd);
      });
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Text(
      _displayText,
      style: widget.style,
      textAlign: widget.textAlign,
    );
  }
}
