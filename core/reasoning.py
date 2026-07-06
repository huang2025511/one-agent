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
        parts = ["【深度思考模式】\n"]
        parts.append(f"用户任务：{task}\n")

        parts.append("请按以下步骤思考（思考过程不需要输出给用户，你自己内部推演）：\n")

        step_num = 1

        # Step 1: Understand the problem
        parts.append(f"{step_num}. **问题理解** — 明确用户的核心需求是什么？有哪些约束条件？")
        step_num += 1

        # Step 2: Task decomposition
        if any(t in task_types for t in ("coding", "analysis", "planning")):
            parts.append(f"{step_num}. **任务分解** — 将大任务拆分为3-5个小步骤，每个步骤做什么？")
            step_num += 1

        # Step 3: Tool/resource assessment
        if tools:
            parts.append(f"{step_num}. **工具选择** — 检查可用工具：{', '.join(tools)}。哪些工具可以帮助完成这个任务？如何组合使用？")
            step_num += 1

        # Step 4: Edge cases
        if "debugging" in task_types:
            parts.append(f"{step_num}. **常见问题预判** — 可能会遇到什么错误或边界情况？如何排查？")
            step_num += 1

        # Step 5: Implementation plan
        if "coding" in task_types:
            parts.append(f"{step_num}. **实现方案** — 选择什么技术方案？整体架构是什么？关键函数如何设计？")
            step_num += 1

        # Step 6: Verification
        parts.append(f"{step_num}. **验证方法** — 完成后如何验证结果是否正确？有哪些检查点？")
        step_num += 1

        # Step 7: Final answer
        parts.append(f"{step_num}. **组织答案** — 以清晰、有条理的方式输出最终结果。\n")

        parts.append("思考完成后，直接给出最终答案。如果需要调用工具，在思考完工具调用方案后立即调用。")

        return "\n".join(parts)

    def _generate_prompt_en(
        self, task: str, task_types: List[str], tools: List[str]
    ) -> str:
        parts = ["【Deep Thinking Mode】\n"]
        parts.append(f"User task: {task}\n")

        parts.append("Think step by step (internal reasoning, don't show to user):\n")

        step_num = 1

        parts.append(f"{step_num}. **Problem Understanding** — What's the core requirement? What constraints exist?")
        step_num += 1

        if any(t in task_types for t in ("coding", "analysis", "planning")):
            parts.append(f"{step_num}. **Task Decomposition** — Break into 3-5 subtasks. What does each step do?")
            step_num += 1

        if tools:
            parts.append(f"{step_num}. **Tool Selection** — Available tools: {', '.join(tools)}. Which ones help? How to combine them?")
            step_num += 1

        if "debugging" in task_types:
            parts.append(f"{step_num}. **Edge Case Anticipation** — What errors might occur? How to troubleshoot?")
            step_num += 1

        if "coding" in task_types:
            parts.append(f"{step_num}. **Implementation Plan** — What approach? Architecture? Key functions?")
            step_num += 1

        parts.append(f"{step_num}. **Verification Method** — How to validate the result? Checkpoints?")
        step_num += 1

        parts.append(f"{step_num}. **Final Answer** — Organize and output the final result clearly.\n")

        parts.append("After thinking, give the final answer directly. If tools are needed, call them after planning.")

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
