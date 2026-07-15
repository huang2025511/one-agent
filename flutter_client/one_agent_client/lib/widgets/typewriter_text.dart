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
  String _targetText = '';
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _targetText = widget.text;
    _startTimer();
  }

  @override
  void didUpdateWidget(TypewriterText oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.text != oldWidget.text) {
      _targetText = widget.text;
      // 不取消 timer，让现有 timer 继续追赶新目标
      // 如果已经追上了，重新启动 timer
      if (_timer == null || !_timer!.isActive) {
        _startTimer();
      }
    }
  }

  void _startTimer() {
    _timer?.cancel();
    // 如果已经显示完整文本，无需启动
    if (_displayText.length >= _targetText.length) {
      _displayText = _targetText;
      return;
    }
    _timer = Timer.periodic(widget.speed, (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      final current = _displayText.length;
      if (current >= _targetText.length) {
        timer.cancel();
        return;
      }
      // 每次显示2个字符，快速追赶目标文本
      final nextEnd = (current + 2).clamp(0, _targetText.length);
      setState(() {
        _displayText = _targetText.substring(0, nextEnd);
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
