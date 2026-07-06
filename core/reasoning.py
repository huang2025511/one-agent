"""Step-by-step reasoning — structured Chain-of-Thought (CoT) generation.

Enhances complex problem solving by:
- Breaking complex tasks into subtasks
- Planning before acting
- Verifying each step
- Showing intermediate reasoning (optional)

This complements the existing tiered execution in coordinator with more
structured reasoning patterns for expert-level tasks.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StepByStepReasoner:
    """Structured reasoning engine for complex tasks.

    Provides reasoning templates and strategies that the coordinator can
    use to guide the LLM through complex problems more reliably.
    """

    def __init__(self) -> None:
        pass

    def detect_task_type(self, text: str) -> List[str]:
        """Detect what type(s) of task the user is asking about.

        Uses LLM-based intent classification instead of keyword matching.
        Falls back to heuristic classification when LLM is unavailable.
        """
        from utils.intent_classifier import get_classifier

        classifier = get_classifier()
        return classifier.classify_task_type(text)

    def should_use_cot(self, complexity: float, task_types: List[str]) -> bool:
        """Determine whether Chain-of-Thought reasoning should be used.

        CoT is worth it for:
        - Complex/expert tasks (high complexity)
        - Specific task types (coding, analysis, debugging)
        """
        if complexity >= 0.6:
            return True
        if any(t in task_types for t in ("coding", "analysis", "debugging", "planning")):
            return complexity >= 0.3
        return False

    def generate_reasoning_prompt(
        self,
        task: str,
        task_types: List[str],
        available_tools: Optional[List[str]] = None,
        lang: str = "zh",
    ) -> str:
        """Generate a structured CoT reasoning prompt.

        The prompt guides the LLM to think step-by-step before producing
        a final answer, which improves accuracy on complex tasks.
        """
        available_tools = available_tools or []

        if lang.startswith("zh"):
            prompt = self._generate_prompt_zh(task, task_types, available_tools)
        else:
            prompt = self._generate_prompt_en(task, task_types, available_tools)

        return prompt

    def _generate_prompt_zh(
        self, task: str, task_types: List[str], tools: List[str]
    ) -> str:
        """Gap 修复：自适应 CoT，根据任务类型使用完全不同的推理模板。

        之前是固定模板 + 可选步骤插入，核心步骤（问题理解→验证→答案）
        对所有任务都一样。现在按任务类型匹配最合适的推理深度：
        - chat/简单问答：2 步（省 token）
        - code/analysis：深入推理（5 步）
        - action/system：执行导向（工具优先）
        - design：方案对比（多候选）
        """
        # 简短问答/闲聊 → 最小推理模式
        if all(t in ("chat", "general") for t in task_types):
            return (
                "【快速思考】\n"
                f"用户：{task[:200]}\n"
                "简洁直接地回答，不需要复杂推理。"
            )

        # 执行类任务 → 工具优先
        if any(t in task_types for t in ("action", "system")):
            parts = ["【执行规划】\n"]
            parts.append(f"任务：{task[:300]}\n")
            if tools:
                parts.append(f"可用工具：{', '.join(tools[:8])}\n")
            parts.append("请按以下步骤执行：\n")
            parts.append("1. **目标确认** — 要完成什么？\n")
            parts.append("2. **工具链设计** — 需要哪些工具？调用顺序？\n")
            if tools:
                parts.append("3. **调用工具** — 立即调用需要的工具\n")
            parts.append("4. **结果验证** — 工具返回结果是否满足需求？\n")
            parts.append("5. **最终输出** — 整理结果回复用户\n")
            return "\n".join(parts)

        # 设计类任务 → 方案对比
        if "design" in task_types:
            parts = ["【方案设计】\n"]
            parts.append(f"需求：{task[:300]}\n")
            parts.append("请按以下步骤思考：\n")
            parts.append("1. **需求分析** — 用户真正需要什么？有什么约束？\n")
            parts.append("2. **候选方案** — 提出 2-3 个可行的技术方案\n")
            parts.append("3. **方案对比** — 每个方案的优劣、适用场景\n")
            parts.append("4. **推荐方案** — 选择最佳方案并说明理由\n")
            parts.append("5. **实现要点** — 关键注意事项\n")
            return "\n".join(parts)

        # 代码/分析/调试 → 深度推理（默认）
        parts = ["【深度思考】\n"]
        parts.append(f"任务：{task[:300]}\n")
        if tools:
            parts.append(f"可用工具：{', '.join(tools[:8])}\n")
        parts.append("请按以下步骤推理：\n")
        parts.append("1. **问题理解** — 明确用户需求，识别约束条件\n")
        parts.append("2. **任务分解** — 将任务拆分为 3-5 个可执行步骤\n")
        if tools:
            parts.append("3. **工具选择** — 确定每个步骤需要哪些工具\n")
        if "debugging" in task_types:
            parts.append("3. **常见问题预判** — 可能遇到什么错误？如何排查？\n")
        if "coding" in task_types:
            parts.append("4. **实现方案** — 技术方案、架构设计、关键函数\n")
        parts.append("5. **验证方法** — 如何确认结果正确？\n")
        parts.append("6. **最终答案** — 清晰完整地输出\n")
        if tools:
            parts.append("\n思考完成后，如果需要工具，立即调用。")
        return "\n".join(parts)

    def _generate_prompt_en(
        self, task: str, task_types: List[str], tools: List[str]
    ) -> str:
        """Adaptive CoT: task-type-specific reasoning templates (English)."""
        if all(t in ("chat", "general") for t in task_types):
            return (
                "[Quick Thinking]\n"
                f"User: {task[:200]}\n"
                "Answer concisely and directly. No complex reasoning needed."
            )

        if any(t in task_types for t in ("action", "system")):
            parts = ["[Execution Planning]\n"]
            parts.append(f"Task: {task[:300]}\n")
            if tools:
                parts.append(f"Available tools: {', '.join(tools[:8])}\n")
            parts.append("Execute step by step:\n")
            parts.append("1. **Goal** — What needs to be done?\n")
            parts.append("2. **Tool chain** — Which tools? What order?\n")
            if tools:
                parts.append("3. **Call tools** — Call the needed tools now\n")
            parts.append("4. **Verify** — Do results meet the goal?\n")
            parts.append("5. **Output** — Final answer to user\n")
            return "\n".join(parts)

        if "design" in task_types:
            parts = ["[Design Thinking]\n"]
            parts.append(f"Requirements: {task[:300]}\n")
            parts.append("Think step by step:\n")
            parts.append("1. **Requirements** — What does the user need? Constraints?\n")
            parts.append("2. **Candidates** — Propose 2-3 feasible solutions\n")
            parts.append("3. **Comparison** — Pros and cons of each\n")
            parts.append("4. **Recommendation** — Best option with rationale\n")
            parts.append("5. **Implementation** — Key considerations\n")
            return "\n".join(parts)

        parts = ["[Deep Thinking]\n"]
        parts.append(f"Task: {task[:300]}\n")
        if tools:
            parts.append(f"Available tools: {', '.join(tools[:8])}\n")
        parts.append("Think step by step:\n")
        parts.append("1. **Problem Understanding** — Core requirement and constraints\n")
        parts.append("2. **Task Decomposition** — Break into 3-5 executable steps\n")
        if tools:
            parts.append("3. **Tool Selection** — Which tools for each step?\n")
        if "debugging" in task_types:
            parts.append("3. **Edge Cases** — What errors? How to troubleshoot?\n")
        if "coding" in task_types:
            parts.append("4. **Implementation** — Approach, architecture, key functions\n")
        parts.append("5. **Verification** — How to validate correctness?\n")
        parts.append("6. **Final Answer** — Clear, complete output\n")
        if tools:
            parts.append("\nCall tools immediately after planning if needed.")
        return "\n".join(parts)

    def extract_steps_from_response(self, response: str) -> List[str]:
        """Extract reasoning steps from a response that includes step markers.

        Looks for patterns like:
        - Step 1: ...
        - 第一步: ...
        - 1. ...
        """
        steps = []

        # Match numbered steps
        for pattern in [
            r"步骤\s*(\d+)[:：]\s*(.+?)(?=步骤\s*\d+[:：]|$)",
            r"Step\s*(\d+)[:：]\s*(.+?)(?=Step\s*\d+[:：]|$)",
            r"^(\d+)[.、]\s*(.+?)$",
        ]:
            matches = re.findall(pattern, response, re.MULTILINE | re.DOTALL)
            if matches:
                steps = [m[1].strip() for m in sorted(matches, key=lambda x: int(x[0]))]
                break

        return steps

    def generate_progress_update(
        self,
        current_step: int,
        total_steps: int,
        step_description: str,
        lang: str = "zh",
    ) -> str:
        """Generate a progress update message for long-running tasks.

        Useful when the agent wants to let the user know it's working on
        a complex multi-step problem.
        """
        if lang.startswith("zh"):
            return f"⏳ 进度：{current_step}/{total_steps} — {step_description}"
        else:
            return f"⏳ Progress: {current_step}/{total_steps} — {step_description}"

    def generate_verification_checklist(
        self,
        task_types: List[str],
        lang: str = "zh",
    ) -> List[str]:
        """Generate a verification checklist for a task type.

        This is injected into the conversation to help the LLM verify its
        own work before delivering the final answer.
        """
        checklists = {
            "coding": [
                "代码是否能正常运行？有没有语法错误？",
                "边界情况是否处理了？（空输入、异常输入等）",
                "变量命名是否清晰？有没有冗余代码？",
                "有没有安全问题？（注入、越权等）",
            ],
            "analysis": [
                "数据来源是否可靠？",
                "分析逻辑是否正确？有没有遗漏因素？",
                "结论是否有数据支撑？",
                "有没有其他可能的解释？",
            ],
            "debugging": [
                "根本原因找到并修复了吗？",
                "有没有引入新的问题？",
                "是否测试了正常和异常情况？",
                "有没有预防措施防止再次发生？",
            ],
            "planning": [
                "方案是否可行？有没有资源/时间约束？",
                "风险点识别了吗？有预案吗？",
                "有没有更好的替代方案？",
                "优先级和顺序合理吗？",
            ],
            "learning": [
                "核心概念解释清楚了吗？",
                "有没有实际例子帮助理解？",
                "从易到难的顺序合理吗？",
                "有没有常见误区需要提醒？",
            ],
            "general": [
                "答案是否准确？有没有错误信息？",
                "是否完全回答了用户的问题？",
                "表达是否清晰有条理？",
                "有没有遗漏重要信息？",
            ],
        }

        result = []
        for t in task_types:
            result.extend(checklists.get(t, checklists["general"]))

        if not result:
            result = checklists["general"]

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for item in result:
            if item not in seen:
                seen.add(item)
                unique.append(item)

        return unique[:6]  # Max 6 items


# Singleton
_step_reasoner: Optional[StepByStepReasoner] = None


def get_step_reasoner() -> StepByStepReasoner:
    """Get the shared StepByStepReasoner instance."""
    global _step_reasoner
    if _step_reasoner is None:
        _step_reasoner = StepByStepReasoner()
    return _step_reasoner
