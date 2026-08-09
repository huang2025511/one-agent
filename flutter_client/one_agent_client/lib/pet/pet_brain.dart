import 'dart:async';
import 'dart:math';

/// 宠物情绪状态
enum PetMood {
  idle,      // 待机（呼吸/微动）
  happy,     // 开心
  thinking,  // 思考
  talking,   // 说话中
  sleeping,  // 睡觉
  curious,   // 好奇（左右看）
  excited,   // 兴奋
  sad,       // 难过
}

/// 宠物动作类型
///
/// 每个动作对应 Rive 动画中的一个状态或动画名
enum PetAction {
  idle,        // 待机循环
  blink,       // 眨眼
  lookAround,  // 左右看
  bounce,      // 弹跳
  wave,        // 挥手
  tilt,        // 歪头
  sleep,       // 睡觉
  talk,        // 说话（嘴巴动）
  think,       // 思考
  happy,       // 开心
}

/// 动作配置
class _ActionConfig {
  final PetAction action;
  final Duration duration;   // 动作持续时间
  final double weight;       // 随机选中权重

  const _ActionConfig({
    required this.action,
    required this.duration,
    this.weight = 1.0,
  });
}

/// 宠物大脑 —— 管理自主动作调度
///
/// 参考 Koishi AI Pet 的状态机设计：
/// - idle 状态下定时随机触发小动作（眨眼/歪头/左右看）
/// - 长时间无操作进入睡觉
/// - 对话时进入 thinking → talking
/// - 对话结束回到 happy → idle
///
/// 核心循环：
///   idle → (随机间隔) → 小动作 → idle → ... → (5分钟无操作) → sleep
class PetBrain {
  // 当前情绪
  PetMood _mood = PetMood.idle;
  PetMood get mood => _mood;

  // 当前动作
  PetAction _currentAction = PetAction.idle;
  PetAction get currentAction => _currentAction;

  // 状态变化回调
  void Function(PetMood mood, PetAction action)? onStateChanged;

  // 定时器
  Timer? _idleTimer;       // idle 状态下的随机动作触发器
  Timer? _actionTimer;     // 当前动作的持续时间计时
  Timer? _sleepTimer;      // 长时间无操作进入睡眠

  // 最后一次互动时间
  DateTime _lastInteraction = DateTime.now();

  // idle 状态下可随机触发的小动作
  static const _idleActions = [
    _ActionConfig(action: PetAction.blink,     duration: Duration(milliseconds: 500), weight: 3.0),
    _ActionConfig(action: PetAction.lookAround, duration: Duration(seconds: 2),        weight: 2.0),
    _ActionConfig(action: PetAction.tilt,      duration: Duration(seconds: 1),         weight: 2.0),
    _ActionConfig(action: PetAction.bounce,    duration: Duration(milliseconds: 800),  weight: 1.5),
    _ActionConfig(action: PetAction.wave,      duration: Duration(seconds: 1),         weight: 1.0),
  ];

  // 互动后可能触发的开心动作
  static const _happyActions = [
    _ActionConfig(action: PetAction.happy,  duration: Duration(seconds: 2), weight: 2.0),
    _ActionConfig(action: PetAction.bounce, duration: Duration(seconds: 1), weight: 1.5),
    _ActionConfig(action: PetAction.wave,   duration: Duration(seconds: 1), weight: 1.0),
  ];

  static final _random = Random();

  /// 启动宠物大脑
  void start() {
    _setMood(PetMood.idle);
    _scheduleNextIdleAction();
    _resetSleepTimer();
  }

  /// 停止
  void stop() {
    _idleTimer?.cancel();
    _actionTimer?.cancel();
    _sleepTimer?.cancel();
  }

  /// 用户点击宠物
  void onPetTap() {
    _lastInteraction = DateTime.now();
    _resetSleepTimer();

    if (_mood == PetMood.talking || _mood == PetMood.thinking) {
      return; // 对话中不打断
    }

    // 随机一个开心动作
    _playAction(_pickWeighted(_happyActions));
    _setMood(PetMood.happy);

    // 开心动作结束后回到 idle
    _actionTimer?.cancel();
    _actionTimer = Timer(const Duration(seconds: 2), () {
      _setMood(PetMood.idle);
      _scheduleNextIdleAction();
    });
  }

  /// 开始思考（发送消息后等待回复）
  void startThinking() {
    _lastInteraction = DateTime.now();
    _resetSleepTimer();
    _idleTimer?.cancel();
    _actionTimer?.cancel();
    _setMood(PetMood.thinking);
    _playAction(const _ActionConfig(action: PetAction.think, duration: Duration(seconds: 30)));
  }

  /// 开始说话（收到回复流）
  void startTalking() {
    _lastInteraction = DateTime.now();
    _resetSleepTimer();
    _idleTimer?.cancel();
    _actionTimer?.cancel();
    _setMood(PetMood.talking);
    _playAction(const _ActionConfig(action: PetAction.talk, duration: Duration(seconds: 30)));
  }

  /// 说话结束
  void stopTalking() {
    _lastInteraction = DateTime.now();
    _resetSleepTimer();
    _setMood(PetMood.happy);
    _playAction(const _ActionConfig(action: PetAction.happy, duration: Duration(seconds: 2)));

    _actionTimer = Timer(const Duration(seconds: 2), () {
      _setMood(PetMood.idle);
      _scheduleNextIdleAction();
    });
  }

  /// 用户拖动宠物
  void onPetDrag() {
    _lastInteraction = DateTime.now();
    _resetSleepTimer();
    if (_mood != PetMood.talking && _mood != PetMood.thinking) {
      _setMood(PetMood.excited);
    }
  }

  /// 拖动结束
  void onPetDragEnd() {
    if (_mood == PetMood.excited) {
      _setMood(PetMood.idle);
      _scheduleNextIdleAction();
    }
  }

  // === 内部逻辑 ===

  void _setMood(PetMood newMood) {
    _mood = newMood;
    onStateChanged?.call(_mood, _currentAction);
  }

  void _playAction(_ActionConfig config) {
    _currentAction = config.action;
    onStateChanged?.call(_mood, _currentAction);
  }

  /// 调度下一次随机小动作
  void _scheduleNextIdleAction() {
    _idleTimer?.cancel();

    // 随机 3~8 秒后触发一个小动作
    final delay = Duration(seconds: 3 + _random.nextInt(6));
    _idleTimer = Timer(delay, () {
      if (_mood != PetMood.idle) return;

      final action = _pickWeighted(_idleActions);
      _playAction(action);

      // 动作持续一段时间后回到 idle
      _actionTimer?.cancel();
      _actionTimer = Timer(action.duration, () {
        if (_mood != PetMood.idle) return;
        _currentAction = PetAction.idle;
        onStateChanged?.call(_mood, _currentAction);
        _scheduleNextIdleAction();
      });
    });
  }

  /// 重置睡眠计时器（5分钟无操作 → 睡觉）
  void _resetSleepTimer() {
    _sleepTimer?.cancel();
    _sleepTimer = Timer(const Duration(minutes: 5), () {
      if (_mood == PetMood.talking || _mood == PetMood.thinking) return;
      _idleTimer?.cancel();
      _actionTimer?.cancel();
      _setMood(PetMood.sleeping);
      _playAction(const _ActionConfig(action: PetAction.sleep, duration: Duration(hours: 1)));
    });
  }

  /// 加权随机选择
  _ActionConfig _pickWeighted(List<_ActionConfig> actions) {
    final totalWeight = actions.fold(0.0, (sum, a) => sum + a.weight);
    var r = _random.nextDouble() * totalWeight;
    for (final action in actions) {
      r -= action.weight;
      if (r <= 0) return action;
    }
    return actions.last;
  }
}
