import 'dart:async';

import 'package:flutter/material.dart';

class TypewriterText extends StatefulWidget {
  final String text;
  final TextStyle? style;
  final TextAlign? textAlign;
  final Duration speed;
  // 修复：流式状态标志。流结束（isStreaming==false）时立即跳到完整文本，
  // 避免长回复追赶延迟。默认 true 保持向后兼容。
  final bool isStreaming;

  const TypewriterText(
    this.text, {
    super.key,
    this.style,
    this.textAlign,
    this.speed = const Duration(milliseconds: 20),
    this.isStreaming = true,
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
    // 修复：流结束（isStreaming==false）时直接显示完整文本，不启动 timer
    if (!widget.isStreaming) {
      _displayText = _targetText;
    } else {
      _startTimer();
    }
  }

  @override
  void didUpdateWidget(TypewriterText oldWidget) {
    super.didUpdateWidget(oldWidget);
    _targetText = widget.text;
    // 修复：流结束（isStreaming==false）时立即跳到完整文本，取消追赶 timer
    if (!widget.isStreaming && _displayText.length < _targetText.length) {
      _displayText = _targetText;
      _timer?.cancel();
      _timer = null;
      return;
    }
    // 不取消 timer，让现有 timer 继续追赶新目标
    // 如果已经追上了，重新启动 timer
    if (widget.text != oldWidget.text &&
        (_timer == null || !_timer!.isActive)) {
      _startTimer();
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
      // 修复：自适应速度——落后过多时按 diff/10 推进（至少 5 字符），快速追赶；
      // 之前固定每次 2 字符（100 字/秒），长回复需百秒才能追上
      final diff = _targetText.length - current;
      final advance = diff > 50 ? (diff / 10).ceil().clamp(5, diff) : 2;
      final nextEnd = (current + advance).clamp(0, _targetText.length);
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
