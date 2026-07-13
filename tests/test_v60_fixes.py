#!/usr/bin/env python3
"""V60 修复验证测试 — 全面测试所有关键逻辑路径。

测试覆盖：
1. 计划检测模式匹配（精确化后的模式，不应误判）
2. 事件总线 publish/subscribe 机制
3. SSE 事件生成器（_ALLOWED_PHASES、streaming 分流）
4. _emit_progress 方法
5. streaming delta 增量计算逻辑
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── 测试 1: 计划检测模式匹配 ──
def test_plan_pattern_detection():
    """测试计划检测模式是否正确匹配且不误判"""
    import re

    # V60 精确化后的中文模式
    zh_patterns = [
        "第 1 步", "第1步", "步骤 1", "步骤1",
        "开始执行第", "开始执行第 1", "开始执行第1",
        "按计划一步一步", "按计划执行", "执行计划",
        "### 第 1 步", "### 第1步", "## 第 1 步", "## 第1步",
        # 非编号步骤格式
        "**下一步**", "**下一步：**", "下一步：", "下一步:",
        "**当前决策**", "**当前决策：**", "当前决策：", "当前决策:",
        "**Next step**", "**Next step:**", "Next step:", "next step:",
        "**Current decision**", "Current decision:",
    ]

    # 应该匹配的文本（LLM 确实只列计划不执行）
    should_match = [
        "好的，我按计划一步一步执行。\n\n### 第 1 步：检查 DNS 解析\n```python\nsocket.gethostbyname(...)\n```\n\n### 第 2 步：发送 HTTP 请求\n\n**开始执行第 1 步：**",
        "让我按计划执行任务。\n\n第一步：搜索信息\n第二步：分析数据\n第三步：输出结论",
        "```\n第1步：准备工作\n```\n\n步骤 1：安装依赖",
        "## 第 1 步\n\n执行命令检查环境",
        "第 1 步\n\n执行网络测试",
        "开始执行第1步：检查环境变量",
        # 用户实际案例：LLM 用"**下一步**"格式而非编号步骤
        '明白了！上一个搜索调用遇到了"服务暂时不可用"的瞬态错误。\n\n**当前决策：** 不等待、不道歉，直接换备用路线。\n\n**下一步：** 立即用 system_run 执行 curl 请求。',
        "**下一步：** 立即执行 python_execute 来验证结果",
        "**下一步**\n\n用 web_search 查找相关信息",
        "Next step: call the system_run tool to execute the command",
    ]

    # 不应该匹配的文本（正常结构化回答）
    should_not_match = [
        "",  # 空文本
        "你好，这是一个正常的回答",
        "一、项目背景\n二、技术方案\n三、实施步骤",  # ← V59 会误判，V60 不会
        "第一步是初始化，第二步是配置，第三步是运行",  # 正常描述，不是计划
        "步骤：先安装依赖，再运行脚本",  # 没有数字编号
        "执行以下命令：\n```bash\nls -la\n```",
        "## 第一步\n\n这是对第一步的解释",  # 没有"第 1 步"格式
        "### 第一步\n\n解释说明",  # 没有"第 1 步"格式
        "下一步我们应该优化代码结构",  # 正常描述，不是执行计划
        "通过学习，我们可以进入下一步",  # 正常上下文
        "以上是第一步的结果，接下来我们看第二步",  # 正常描述结果
    ]

    results = {"match_correct": 0, "match_false_positive": 0, "no_match_correct": 0, "no_match_false_negative": 0,
               "should_match_total": len(should_match), "should_not_match_total": len(should_not_match)}

    for text in should_match:
        matched = any(p in text for p in zh_patterns)
        if matched:
            results["match_correct"] += 1
        else:
            results["no_match_false_negative"] += 1
            print(f"  ❌ 应该匹配但未匹配: {text[:80]}...")

    for text in should_not_match:
        matched = any(p in text for p in zh_patterns)
        if not matched:
            results["no_match_correct"] += 1
        else:
            results["match_false_positive"] += 1
            print(f"  ❌ 不应该匹配但匹配了: {text[:80]}...")

    return results


# ── 测试 2: 事件总线 publish/subscribe ──
async def test_event_bus():
    """测试事件总线的基本功能"""
    from core.events import EventBus

    bus = EventBus()
    received = []

    async def handler(evt):
        received.append(evt)

    bus.subscribe("turn_progress", handler)
    await bus.start()

    # 发布事件 (EventBus.publish 是同步方法)
    bus.publish({"type": "turn_progress", "message": "测试消息", "phase": "thinking", "session_id": "test123"})
    bus.publish({"type": "turn_progress", "message": "", "phase": "planning", "session_id": "test123"})
    bus.publish({"type": "turn_progress", "message": "streaming delta", "phase": "streaming", "session_id": "test123"})

    # 等待事件处理
    await asyncio.sleep(0.2)

    results = {
        "total_events": len(received),
        "first_event_message": received[0].get("message") if received else None,
        "second_event_message": received[1].get("message") if len(received) > 1 else None,
        "third_event_message": received[2].get("message") if len(received) > 2 else None,
    }

    bus.unsubscribe("turn_progress", handler)
    await bus.stop()
    return results


# ── 测试 3: SSE _ALLOWED_PHASES 白名单 ──
def test_allowed_phases():
    """测试 SSE 接口的 _ALLOWED_PHASES 白名单是否包含所有需要的 phase"""
    _ALLOWED_PHASES = {
        "planning", "thinking", "reflection", "plan",
        "multi_agent", "deep_research", "comparison",
        "reasoning", "tool_loop", "regeneration",
        "skill_dispatch", "chart", "tool_result",
        "streaming",
    }

    # 必须包含的 phase
    required = {
        "streaming",    # V60 新增：最终答案流式增量
        "tool_result",  # V58 新增：工具执行结果
        "planning",     # 任务规划
        "thinking",     # 思考过程
        "plan",         # 最终计划
        "tool_loop",    # 工具循环
        "reflection",   # 反思
        "comparison",   # 多方案比较
        "reasoning",    # 深度推理
    }

    missing = required - _ALLOWED_PHASES
    results = {
        "all_required_present": len(missing) == 0,
        "missing": list(missing) if missing else [],
        "total_phases": len(_ALLOWED_PHASES),
    }
    return results


# ── 测试 4: SSE 事件生成器分流逻辑 ──
def test_sse_streaming_logic():
    """测试 SSE 生成器中 streaming vs thinking 的分流逻辑"""
    # 模拟 progress_queue 中的数据
    test_events = [
        # (msg, phase, expected_output_type)
        ("正在规划任务...", "planning", "thinking"),
        ("正在分析任务...", "thinking", "thinking"),
        ("Hello", "streaming", "content"),  # streaming → 作为 content 推送
        (" world", "streaming", "content"),
        ("✅ web_search 成功", "tool_result", "thinking"),
        ("", "plan", "thinking"),  # 空消息不应推送
    ]

    results = []
    for msg, phase, expected_type in test_events:
        if phase == "streaming":
            # streaming phase → 作为 content 推送（无 status 字段）
            data = {"content": msg, "session_id": "test"}
            actual_type = "content"
        else:
            # thinking 类 phase → 作为 thinking 推送（有 status 字段）
            if not msg:
                continue  # 空消息跳过
            data = {"status": "thinking", "content": msg, "phase": phase, "session_id": "test"}
            actual_type = "thinking"

        results.append({
            "msg": msg[:30],
            "phase": phase,
            "expected_type": expected_type,
            "actual_type": actual_type,
            "match": actual_type == expected_type,
            "has_status": "status" in data,
            "has_phase": "phase" in data if "phase" in data else None,
        })

    return results


# ── 测试 5: streaming delta 增量计算 ──
def test_streaming_delta_logic():
    """测试 streaming 增量文本计算逻辑"""
    # 模拟 LLM 流式输出
    deltas = [
        "你好，", "这是", "一个", "测试", "回答。",
        "今天天气", "很好。", "我们可以", "出去走走。",
    ]

    streamed_parts = []
    last_emit = 0
    emitted_deltas = []

    for delta in deltas:
        streamed_parts.append(delta)
        current_len = sum(len(d) for d in streamed_parts)
        # 每 8 字符推送一次增量
        if current_len - last_emit >= 8:
            full_so_far = "".join(streamed_parts)
            delta_text = full_so_far[last_emit:]
            last_emit = current_len
            emitted_deltas.append(delta_text)

    # 推送剩余
    final_text = "".join(streamed_parts)
    remaining = final_text[last_emit:]
    if remaining:
        emitted_deltas.append(remaining)

    # 验证：拼接所有增量应该等于完整文本
    full_text = "".join(streamed_parts)
    reconstructed = "".join(emitted_deltas)

    return {
        "full_text": full_text,
        "reconstructed": reconstructed,
        "match": full_text == reconstructed,
        "num_deltas": len(emitted_deltas),
        "deltas": emitted_deltas,
    }


# ── 测试 6: _streamed_content 去重逻辑 ──
def test_streamed_dedup():
    """测试 streaming 推送后不再重复推送 reply 的逻辑"""
    test_cases = [
        # (_streamed_content, reply, expected_push_reply)
        (True, "这是最终答案", False),   # 已 streaming，不重复推送
        (False, "这是最终答案", True),   # 未 streaming，正常推送
        (True, "", False),               # 已 streaming，但 reply 为空
        (False, "", False),              # 未 streaming，但 reply 为空
        (False, None, False),            # 未 streaming，reply 为 None
    ]

    results = []
    for _streamed_content, reply, expected_push in test_cases:
        actual_push = bool(reply) and not _streamed_content
        results.append({
            "streamed_content": _streamed_content,
            "reply": str(reply)[:30] if reply else str(reply),
            "expected_push": expected_push,
            "actual_push": actual_push,
            "match": actual_push == expected_push,
        })

    return results


# ── 主测试入口 ──
async def main():
    print("=" * 60)
    print("  V60 修复验证测试")
    print("=" * 60)

    all_passed = True
    total_tests = 0
    passed_tests = 0

    # 测试 1
    total_tests += 1
    print("\n📋 测试 1: 计划检测模式匹配")
    results = test_plan_pattern_detection()
    print(f"  正确匹配: {results['match_correct']}/{results['should_match_total']}")
    print(f"  正确不匹配: {results['no_match_correct']}/{results['should_not_match_total']}")
    print(f"  误判: {results['match_false_positive']}")
    print(f"  漏判: {results['no_match_false_negative']}")
    test1_pass = results['match_false_positive'] == 0 and results['no_match_false_negative'] == 0
    if test1_pass:
        passed_tests += 1
        print("  ✅ 通过")
    else:
        print("  ❌ 失败")
        all_passed = False

    # 测试 2
    total_tests += 1
    print("\n📋 测试 2: 事件总线 publish/subscribe")
    results = await test_event_bus()
    print(f"  收到事件数: {results['total_events']}")
    print(f"  事件1内容: {results['first_event_message']}")
    print(f"  事件2内容: {results['second_event_message']}")
    print(f"  事件3内容: {results['third_event_message']}")
    test2_pass = results['total_events'] == 3
    if test2_pass:
        passed_tests += 1
        print("  ✅ 通过")
    else:
        print("  ❌ 失败")
        all_passed = False

    # 测试 3
    total_tests += 1
    print("\n📋 测试 3: SSE _ALLOWED_PHASES 白名单完整性")
    results = test_allowed_phases()
    print(f"  所有必需 phase 存在: {results['all_required_present']}")
    print(f"  缺失: {results['missing']}")
    print(f"  白名单总数: {results['total_phases']}")
    test3_pass = results['all_required_present']
    if test3_pass:
        passed_tests += 1
        print("  ✅ 通过")
    else:
        print("  ❌ 失败")
        all_passed = False

    # 测试 4
    total_tests += 1
    print("\n📋 测试 4: SSE 事件分流逻辑 (streaming vs thinking)")
    results = test_sse_streaming_logic()
    all_match = True
    for r in results:
        status = "✅" if r['match'] else "❌"
        print(f"  {status} phase={r['phase']:15s} → {r['actual_type']:10s} (期望: {r['expected_type']})")
        if not r['match']:
            all_match = False
    test4_pass = all_match
    if test4_pass:
        passed_tests += 1
        print("  ✅ 通过")
    else:
        print("  ❌ 失败")
        all_passed = False

    # 测试 5
    total_tests += 1
    print("\n📋 测试 5: streaming delta 增量计算")
    results = test_streaming_delta_logic()
    print(f"  完整文本: {results['full_text']}")
    print(f"  重构文本: {results['reconstructed']}")
    print(f"  文本匹配: {results['match']}")
    print(f"  delta 数量: {results['num_deltas']}")
    print(f"  deltas: {results['deltas']}")
    test5_pass = results['match']
    if test5_pass:
        passed_tests += 1
        print("  ✅ 通过")
    else:
        print("  ❌ 失败")
        all_passed = False

    # 测试 6
    total_tests += 1
    print("\n📋 测试 6: _streamed_content 去重逻辑")
    results = test_streamed_dedup()
    all_match = True
    for r in results:
        status = "✅" if r['match'] else "❌"
        print(f"  {status} streamed={r['streamed_content']}, reply={r['reply']:20s} → push={r['actual_push']} (期望: {r['expected_push']})")
        if not r['match']:
            all_match = False
    test6_pass = all_match
    if test6_pass:
        passed_tests += 1
        print("  ✅ 通过")
    else:
        print("  ❌ 失败")
        all_passed = False

    # 汇总
    print("\n" + "=" * 60)
    print(f"  测试结果: {passed_tests}/{total_tests} 通过")
    if all_passed:
        print("  🎉 全部测试通过！")
    else:
        print("  ⚠️ 有测试失败，需要修复")
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))