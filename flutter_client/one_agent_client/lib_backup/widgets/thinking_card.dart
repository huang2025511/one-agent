import 'package:flutter/material.dart';

/// 思考过程展示卡片（可展开/折叠）
class ThinkingCard extends StatefulWidget {
  final String thinking;

  const ThinkingCard({super.key, required this.thinking});

  @override
  State<ThinkingCard> createState() => _ThinkingCardState();
}

class _ThinkingCardState extends State<ThinkingCard> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: EdgeInsets.zero,
      color: Colors.amber.shade50.withOpacity(0.5),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(12),
        side: BorderSide(color: Colors.amber.shade200),
      ),
      child: InkWell(
        onTap: () => setState(() => _expanded = !_expanded),
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(
                    Icons.psychology_outlined,
                    size: 16,
                    color: Colors.amber.shade800,
                  ),
                  const SizedBox(width: 6),
                  Text(
                    '思考过程',
                    style: TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                      color: Colors.amber.shade800,
                    ),
                  ),
                  const Spacer(),
                  AnimatedRotation(
                    turns: _expanded ? 0.5 : 0,
                    duration: const Duration(milliseconds: 200),
                    child: Icon(
                      Icons.keyboard_arrow_down,
                      size: 18,
                      color: Colors.amber.shade800,
                    ),
                  ),
                ],
              ),
              if (_expanded) ...[
                const SizedBox(height: 8),
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Colors.white.withOpacity(0.6),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    widget.thinking,
                    style: TextStyle(
                      fontSize: 13,
                      height: 1.5,
                      color: Colors.amber.shade900,
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
