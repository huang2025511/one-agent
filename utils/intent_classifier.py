"""Universal LLM-based intent classification.

Replaces keyword/regex matching with intelligent LLM-based classification
across the entire one-agent project.

Features:
- Fast path for trivial inputs (greetings, short responses)
- LLM classification for everything else
- Built-in caching to avoid duplicate LLM calls
- Fallback to heuristic when LLM fails
- Unified API for different classification tasks
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class IntentClassifier:
    """Universal intent classifier using LLM.

    Supports multiple classification modes:
    - complexity: 0-1 complexity score for routing
    - task_type: coding/analysis/planning/learning/debugging/general
    - model_filter: free_only, min_context, input_modality, etc.
    - tool_needs: whether tools/system access is needed

    Design:
    1. Fast path: short/trivial inputs skip LLM entirely
    2. LLM classification: use cheapest available model
    3. Cache: MD5 hash + TTL, deduplicate same inputs
    4. Fallback: heuristic as safety net
    """

    _cache: Dict[str, tuple] = {}
    _CACHE_TTL = 3600
    _CACHE_MAX = 500

    def __init__(self, llm_provider: Any = None) -> None:
        self._llm = llm_provider
        self._fast_path_patterns: Dict[str, re.Pattern] = {
            "greeting": re.compile(r"^(你好|嗨|hi|hello|hey|哈喽|在吗|早上好|下午好)$", re.IGNORECASE),
            "ack": re.compile(r"^(谢谢|thanks|ok|好的|嗯|行|可以|收到)$", re.IGNORECASE),
            "exit": re.compile(r"^(再见|bye|exit|quit|q)$", re.IGNORECASE),
        }

    def classify_complexity(self, text: str) -> Tuple[float, Dict[str, Any]]:
        """Classify complexity and intent.

        Returns (complexity: float, meta: dict).
        Synchronous version - uses fallback heuristic when LLM is not available.
        """
        t = text.strip()
        if not t:
            return 0.0, {"intent_source": "empty"}

        fast_result = self._check_fast_path(t, "complexity")
        if fast_result is not None:
            return fast_result

        return self._fallback_classify(t, "complexity")

    def classify_task_type(self, text: str) -> List[str]:
        """Classify task types (coding/analysis/planning/learning/debugging).

        Synchronous version - uses fallback heuristic when LLM is not available.
        """
        t = text.strip()
        if not t:
            return ["general"]

        fast_result = self._check_fast_path(t, "task_type")
        if fast_result is not None:
            _, meta = fast_result
            return meta.get("task_types", ["general"])

        _, meta = self._fallback_classify(t, "task_type")
        return meta.get("task_types", ["general"])

    def classify_model_filter(self, text: str) -> Dict[str, Any]:
        """Classify model filter spec from user input.

        Synchronous version - uses fallback heuristic when LLM is not available.
        """
        t = text.strip()
        if not t:
            return {}

        _, meta = self._fallback_classify(t, "model_filter")
        return meta.get("filter_spec", {})

    async def classify_complexity_async(self, text: str) -> Tuple[float, Dict[str, Any]]:
        """Async version of classify_complexity - uses LLM when available."""
        return await self._classify(text, mode="complexity")

    async def classify_task_type_async(self, text: str) -> List[str]:
        """Async version of classify_task_type - uses LLM when available."""
        _, meta = await self._classify(text, mode="task_type")
        return meta.get("task_types", ["general"])

    async def classify_model_filter_async(self, text: str) -> Dict[str, Any]:
        """Async version of classify_model_filter - uses LLM when available."""
        _, meta = await self._classify(text, mode="model_filter")
        return meta.get("filter_spec", {})

    def classify_cli_command(self, text: str) -> Optional[str]:
        """Classify CLI command intent."""
        return self._classify_cli(text)

    def classify_command_risk(self, command: str) -> int:
        """Classify shell command risk (0=safe, 1=low, 2=medium, 3=dangerous)."""
        return self._classify_risk(command)

    # -------------------------------------------------------------------------

    async def _classify(self, text: str, mode: str) -> Tuple[float, Dict[str, Any]]:
        """Core classification logic."""
        t = text.strip()
        if not t:
            return 0.0, {"intent_source": "empty"}

        fast_result = self._check_fast_path(t, mode)
        if fast_result is not None:
            return fast_result

        cache_key = self._make_cache_key(t, mode)
        cached = self._cache.get(cache_key)
        if cached:
            complexity, meta, ts = cached
            if time.time() - ts < self._CACHE_TTL:
                meta = {**meta, "intent_source": "cache"}
                return complexity, meta

        if self._llm is None:
            return self._fallback_classify(t, mode)

        try:
            complexity, meta = await self._llm_classify(t, mode)
        except Exception:
            logger.exception("LLM intent classification failed for mode=%s", mode)
            return self._fallback_classify(t, mode)

        self._cache_result(cache_key, complexity, meta)

        return complexity, meta

    def _check_fast_path(self, text: str, mode: str) -> Optional[Tuple[float, Dict[str, Any]]]:
        """Fast path for trivial inputs - no LLM call."""
        if len(text) <= 15:
            if self._fast_path_patterns["greeting"].match(text):
                return 0.05, {
                    "needs_tools": False,
                    "needs_system": False,
                    "task_types": ["general"],
                    "intent_source": "fast_path",
                }
            if self._fast_path_patterns["ack"].match(text):
                return 0.1, {
                    "needs_tools": False,
                    "needs_system": False,
                    "task_types": ["general"],
                    "intent_source": "fast_path",
                }
            if self._fast_path_patterns["exit"].match(text):
                return 0.05, {
                    "needs_tools": False,
                    "needs_system": False,
                    "task_types": ["general"],
                    "intent_source": "fast_path",
                }
        return None

    async def _llm_classify(self, text: str, mode: str) -> Tuple[float, Dict[str, Any]]:
        """Use LLM to classify intent."""
        prompts = {
            "complexity": (
                "分析用户输入的意图和复杂度，返回JSON。\n"
                "字段：\n"
                "- complexity: 0.0-1.0（0=闲聊问候, 0.3=简单问答, 0.5=需要思考/设计/分析, 0.8=专家级复杂任务）\n"
                "- needs_tools: 是否需要工具（搜索/计算/执行命令/读写文件）\n"
                "- needs_system: 是否需要操作系统（git/文件/命令行/服务器）\n"
                "- task_types: 任务类型列表，可选值: chat, coding, analysis, planning, learning, debugging, action, system\n"
                f"\n用户输入：{text[:500]}\n\n"
                '只返回JSON，示例：{"complexity": 0.7, "needs_tools": true, "needs_system": false, "task_types": ["design", "planning"]}'
            ),
            "task_type": (
                "分析用户输入的任务类型，返回JSON。\n"
                "字段：\n"
                "- task_types: 任务类型列表，可选值: coding(代码/编程/实现), analysis(分析/研究/比较), planning(计划/方案/设计), learning(学习/教程/解释), debugging(调试/bug/修复), general(其他)\n"
                f"\n用户输入：{text[:500]}\n\n"
                '只返回JSON，示例：{"task_types": ["coding", "planning"]}'
            ),
            "model_filter": (
                "分析用户输入中关于模型选择的要求，返回JSON。\n"
                "字段：\n"
                "- free_only: bool, 是否只要免费模型\n"
                "- paid_only: bool, 是否只要付费模型\n"
                "- min_context: int, 最小上下文长度（如200k=200000）\n"
                "- input_modality: str, 输入模态（image/vision表示需要视觉能力）\n"
                "- feature: str, 特殊能力（reasoning/tools）\n"
                "- tier: str, 模型层级（trivial/simple/complex/expert）\n"
                f"\n用户输入：{text[:500]}\n\n"
                '只返回JSON，示例：{"free_only": true, "min_context": 200000}'
            ),
        }

        prompt = prompts.get(mode, prompts["complexity"])

        result = await self._llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=None,
            temperature=0.0,
            max_tokens=100,
            tools=None,
            use_cache=True,
        )

        raw = (result.get("text") or "").strip()
        data = self._parse_json_response(raw)

        if mode == "complexity":
            complexity = float(data.get("complexity", 0.3))
            meta = {
                "needs_tools": bool(data.get("needs_tools", False)),
                "needs_system": bool(data.get("needs_system", False)),
                "task_types": data.get("task_types", ["general"]),
                "intent_source": "llm",
            }
        elif mode == "task_type":
            complexity = 0.3
            meta = {
                "task_types": data.get("task_types", ["general"]),
                "intent_source": "llm",
            }
        elif mode == "model_filter":
            complexity = 0.2
            meta = {
                "filter_spec": {
                    "free_only": bool(data.get("free_only", False)),
                    "paid_only": bool(data.get("paid_only", False)),
                    "min_context": data.get("min_context"),
                    "input_modality": data.get("input_modality"),
                    "feature": data.get("feature"),
                    "tier": data.get("tier"),
                },
                "intent_source": "llm",
            }
        else:
            complexity = 0.3
            meta = {"intent_source": "llm"}

        complexity = max(0.0, min(1.0, complexity))
        return complexity, meta

    def _parse_json_response(self, raw: str) -> Dict[str, Any]:
        """Parse JSON from LLM response, handling common formatting issues."""
        json_str = raw

        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
            if m:
                json_str = m.group(1)

        if not json_str.startswith("{"):
            start = json_str.find("{")
            end = json_str.rfind("}")
            if start >= 0 and end > start:
                json_str = json_str[start:end + 1]

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON from LLM: %s", raw[:100])
            return {}

    def _fallback_classify(self, text: str, mode: str) -> Tuple[float, Dict[str, Any]]:
        """Heuristic fallback when LLM is unavailable."""
        if mode == "complexity":
            return self._fallback_complexity(text)
        elif mode == "task_type":
            return self._fallback_task_type(text)
        elif mode == "model_filter":
            return self._fallback_model_filter(text)
        return 0.3, {"intent_source": "fallback"}

    def _fallback_complexity(self, text: str) -> Tuple[float, Dict[str, Any]]:
        """Heuristic complexity classification.

        Preserves the same scoring logic as the original router heuristic
        to ensure consistent behavior when LLM is unavailable.
        """
        t = text.strip()
        if not t:
            return 0.0, {"intent_source": "empty"}
        score = 0.1
        lower = t.lower()
        length = len(t)

        # Length-based scoring
        if length > 400:
            score += 0.25
        if length > 1500:
            score += 0.25

        # Expert-level keywords
        for kw in ("optimize", "performance", "debug", "deadlock", "algorithm",
                   "审计", "优化", "性能", "死锁", "算法", "数学", "证明",
                   "deep learning", "neural network", "reinforcement learning",
                   "深度学习", "神经网络", "强化学习"):
            if kw in lower:
                score += 0.35
                break

        # Complex-level keywords
        for kw in ("how to", "如何", "怎样", "设计", "方案", "规划", "步骤",
                   "流程", "分析", "比较", "对比", "build", "create", "develop",
                   "implement", "solve", "fix", "explain", "analyze", "compare"):
            if kw in lower:
                score += 0.2
                break

        # Code keywords
        for kw in ("python", "javascript", "typescript", "rust", "c++", "java",
                   "go", "shell", "bash", "docker", "代码", "编程", "写代码"):
            if kw in lower:
                score += 0.15
                break

        # Complexity boost keywords
        for kw in ("详细", "深入", "完整", "全面", "严谨", "精确", "准确", "具体"):
            if kw in lower:
                score += 0.1
                break

        # Action verbs — need tools, at least simple level
        for kw in ("对比", "检查", "验证", "测试", "执行", "运行", "安装", "下载",
                   "部署", "同步", "推送", "提交", "更新", "查看", "读取", "写入",
                   "修改", "删除", "创建", "生成", "编译", "打包", "搜索",
                   "开发", "实现", "编写", "搭建", "配置", "调试", "排查", "修复",
                   "compare", "check", "verify", "test", "run", "execute",
                   "install", "download", "deploy", "sync", "push", "commit",
                   "update", "read", "write", "modify", "delete", "create",
                   "generate", "compile", "build", "pack", "search", "develop",
                   "implement", "debug", "fix"):
            if kw in lower:
                score += 0.2
                break

        # System operation keywords — need system_run
        for kw in ("gitee", "github", "git", "repo", "仓库", "文件", "目录",
                   "文件夹", "路径", "本地", "远程", "服务器", "终端", "命令行",
                   "shell", "bash", "config", "配置", ".py", ".js", ".ts",
                   ".yaml", ".json", ".md"):
            if kw in lower:
                score += 0.15
                break

        # Trivial keywords — reduce score for short inputs
        for kw in ("hi", "hello", "hey", "ok", "thanks", "thank you",
                   "what's", "what is", "when", "where", "who",
                   "天气", "时间", "日期", "你好", "谢谢", "再见", "help", "?"):
            if kw in lower and length < 60:
                score -= 0.3
                break

        # Paragraph count
        paragraphs = sum(1 for p in t.split("\n") if p.strip())
        score += min(0.1, paragraphs * 0.02)

        score = max(0.0, min(1.0, score))
        return score, {
            "needs_tools": False,
            "needs_system": False,
            "task_types": ["general"],
            "intent_source": "fallback",
        }

    def _fallback_task_type(self, text: str) -> Tuple[float, Dict[str, Any]]:
        """Heuristic task type classification."""
        t = text.lower()
        types = []

        patterns = {
            "coding": [r"代码|编程|实现|开发|写一个|函数|类|脚本|程序", r"code|implement|function|class|script|program|develop"],
            "analysis": [r"分析|研究|调查|评估|比较|对比|测试", r"analyze|analysis|research|investigate|evaluate|compare|test"],
            "planning": [r"计划|方案|设计|步骤|流程|怎么.*做|如何.*实现", r"plan|design|steps|how to|approach|strategy"],
            "learning": [r"学习|教程|入门|基础|讲解|解释.*原理", r"learn|tutorial|explain|how does|understand"],
            "debugging": [r"调试|bug|错误|问题|修复|为什么.*不行|解决", r"debug|error|fix|why.*not|problem|issue|resolve"],
        }

        for task_type, regexes in patterns.items():
            for regex in regexes:
                if re.search(regex, t):
                    types.append(task_type)
                    break

        return 0.3, {"task_types": types if types else ["general"], "intent_source": "fallback"}

    def _fallback_model_filter(self, text: str) -> Tuple[float, Dict[str, Any]]:
        """Heuristic model filter classification."""
        spec: Dict[str, Any] = {}
        t = text.lower()

        if any(k in t for k in ("free", "免费", "试用")):
            spec["free_only"] = True
        if any(k in t for k in ("paid", "收费", "付费")):
            spec["paid_only"] = True
        m = re.search(r"(\d+)\s*k\b", t)
        if m:
            spec["min_context"] = int(m.group(1)) * 1000
        if any(k in t for k in ("vision", "视觉", "image", "图像", "多模态")):
            spec["input_modality"] = "image"
        if any(k in t for k in ("reasoning", "推理", "思考", "thinking")):
            spec["feature"] = "reasoning"
        if any(k in t for k in ("tool", "工具", "function")):
            spec["feature"] = spec.get("feature") or "tools"
        for tier in ("trivial", "simple", "complex", "expert"):
            if tier in t or f"{tier} tier" in t or f"{tier}层" in t:
                spec["tier"] = tier
                break

        return 0.2, {"filter_spec": spec, "intent_source": "fallback"}

    def _classify_cli(self, text: str) -> Optional[str]:
        """CLI command classification."""
        lower = text.lower().strip()
        exact_map = {
            "exit": "exit", "quit": "exit", "q": "exit",
            "help": "help", "?": "help",
            "skills": "skills", "status": "status",
            "metrics": "metrics", "stats": "metrics",
            "dlq": "dlq", "bus": "bus", "clear": "clear",
            "settings": "settings", "config": "settings",
            "models": "models",
        }
        if lower in exact_map:
            return exact_map[lower]

        intent_patterns = {
            "exit": [r"退出|离开|结束|再见|拜拜|关闭|bye|goodbye|see you"],
            "help": [r"帮助|怎么用|使用说明|使用方法|能做什么|有什么功能|命令列表|功能列表|怎么操作"],
            "skills": [r"技能|会什么|能做什么|有哪些能力|有什么技能|工具列表|能力列表|你会啥|你会什么"],
            "status": [r"状态|运行状态|当前状态|系统状态|运行情况|还好吗|活着吗|运行多久"],
            "metrics": [r"指标|统计|性能|调用量|用量|统计数据|性能指标|使用量"],
            "dlq": [r"死信|失败事件|未处理|错误队列|死信队列|dead.?letter|失败的消息"],
            "bus": [r"事件|总线|event.?bus|事件类型|总线状态"],
            "clear": [r"清屏|清除屏幕|清理屏幕|清空|清除|刷新屏幕"],
            "settings": [r"设置|配置|修改设置|查看设置|更改|切换模型|改模型|改温度|开启|关闭|启用|禁用"],
            "models": [r"模型列表|有哪些模型|看模型|列出模型|列出.*模型|所有模型|免费模型|可.*模型"],
            "rebuild_tiers": [r"重建分层|自动分层|自动分配|重新分层|刷新分层|智能分层|rebuild.?tiers|auto.?tier|分层|分类|分档|分配.*模型|模型.*分配"],
        }

        for intent, patterns in intent_patterns.items():
            for pat in patterns:
                if re.search(pat, lower):
                    return intent
        return None

    _DANGEROUS_PATTERNS: List[str] = [
        r"^rm(\s+-[rf]+)+",
        r"^sudo\s",
        r"^chmod\s+[0-7]77\s",
        r"^shutdown\s",
        r"^reboot",
        r"^poweroff",
        r"^mkfs\s",
        r"^dd\s+if=",
        r"^>",
        r">>\s*/dev/",
        r"^mount\s",
        r"^umount\s",
        r"^fdisk\s",
        r"^parted\s",
        r"^mkfs\.",
        r"^wipefs\s",
        r"^crontab\s",
        r"^iptables\s",
        r"\|\s*sh\b",
        r"\|\s*bash\b",
        r"&\s*>/dev/null",
        r";",
        r"&&",
        r"\|\|",
        r"`",
        r"\$\(",
    ]

    _MEDIUM_PATTERNS: List[Tuple[str, str]] = [
        ("pip", r"^pip3?\s+install\s+[\w._-]+(\[[\w,]+\])?$"),
        ("npm", r"^npm\s+install(\s+[\w._@/-]+)?$"),
        ("apt", r"^apt-get\s+(update|install)\s+[\w._-]+$"),
        ("brew", r"^brew\s+install\s+[\w./~_-]+$"),
        ("systemctl", r"^systemctl\s+(start|stop|restart|reload|enable|disable|status)\s+[\w._@-]+$"),
        ("service", r"^service\s+[\w._@-]+\s+(start|stop|restart|status)$"),
        ("docker", r"^docker\s+(start|stop|restart|ps|logs|pull|run)\s+[\w./~_-]+$"),
        ("chown", r"^chown(\s+-R)?\s+[\w:]+\s+[\w./~_-]+$"),
        ("kill", r"^kill(\s+-[0-9]+)?\s+\d+$"),
        ("pkill", r"^pkill(\s+-[0-9]+)?\s+[\w_-]+$"),
    ]

    _LOW_PATTERNS: List[Tuple[str, str]] = [
        ("mkdir", r"^mkdir(\s+-[pv])?\s+[\w./~_-]+$"),
        ("touch", r"^touch\s+[\w./~_-]+$"),
        ("cp", r"^cp(\s+-[rf])?\s+[\w./~_-]+\s+[\w./~_-]+$"),
        ("mv", r"^mv\s+[\w./~_-]+\s+[\w./~_-]+$"),
        ("git", r"^git\s+(status|log|diff|branch|checkout|pull|clone|add|commit|push)\s+[\w./:@#_\"',~ -]+$"),
        ("tar", r"^tar\s+-[cx]\w*\s+[\w./~_-]+(\s+[\w./~_-]+)*$"),
        ("zip", r"^zip\s+[\w./~_-]+\.zip\s+[\w./~_*-]+$"),
        ("unzip", r"^unzip\s+[\w./~_-]+\.zip$"),
        ("tee", r"^tee\s+[\w./~_-]+$"),
    ]

    _SAFE_PATTERNS: List[Tuple[str, str]] = [
        ("cat", r"^cat(\s+-[nb])?\s+[\w./~_-]+$"),
        ("ls", r"^ls(\s+(-[alhRtTr]+|--color=\w+))*(\s+[\w./~_-]+)*$"),
        ("cd", r"^cd\s+[\w./~_-]*$"),
        ("head", r"^head(\s+-n\s+\d+)?\s+[\w./~_-]+$"),
        ("tail", r"^tail(\s+-[nf]\s*\d*)?\s+[\w./~_-]+$"),
        ("wc", r"^wc(\s+-[lwc])?\s+[\w./~_-]+$"),
        ("pwd", r"^pwd(\s+-[LP])?$"),
        ("echo", r"^echo\s+[\w\s./~_-]+$"),
        ("date", r"^date(\s+[+-].*)?$"),
        ("uptime", r"^uptime\s*$"),
        ("free", r"^free(\s+-[hmg])?$"),
        ("df", r"^df(\s+-[hTt])?(\s+[\w./~_-]+)?$"),
        ("du", r"^du(\s+-[hs])?(\s+[\w./~_-]+)?$"),
        ("whoami", r"^whoami\s*$"),
        ("id", r"^id(\s+[\w_-]+)?$"),
        ("uname", r"^uname(\s+-[amnrspvio]+)?$"),
        ("hostname", r"^hostname(\s+-[is])?$"),
        ("env", r"^env(\s+-i)?(\s+[\w_]+=.*)?$"),
        ("which", r"^which\s+[\w_-]+$"),
        ("type", r"^type\s+[\w_-]+$"),
        ("man", r"^man\s+[\w_-]+$"),
        ("find", r"^find\s+[\w./~_-]+\s+(-maxdepth\s+\d+\s+)?(-name\s+[\w.*_-]+| -type\s+[fdl])(\s+( -name\s+[\w.*_-]+| -type\s+[fdl]))*$"),
        ("grep", r"^grep(\s+-[inrvclqwo]+)*\s+[\w\s./~,_\"'-]+$"),
        ("ps", r"^ps(\s+-[auxf]+)?(\s+\|)?$"),
        ("top", r"^top(\s+-[bnHp]+(\s+\d+)?)?$"),
        ("pgrep", r"^pgrep(\s+-[flx])?\s+[\w_-]+$"),
        ("stat", r"^stat\s+[\w./~_-]+$"),
        ("file", r"^file\s+[\w./~_-]+$"),
    ]

    def _classify_risk(self, command: str) -> int:
        """Command risk classification."""
        cmd = command.strip().lower()

        if any(k in cmd for k in ("rm -rf", "rm -fr", "del /f /s /q", "format",
                                   "mkfs", "dd if=", ":(){ :|:& };:", "chmod -R 777",
                                   "shutdown", "reboot", "halt")):
            return 3

        if any(k in cmd for k in ("rm ", "rm *", "rm -r", "rmdir", "del ",
                                   "> /dev/sda", "> /dev/hda", "wget ", "curl ",
                                   "python -c", "python3 -c", "eval ", "exec ",
                                   "sudo ", "su ", "chmod ", "chown ", "passwd ",
                                   "useradd", "userdel")):
            return 2

        if any(k in cmd for k in ("git ", "npm ", "pip ", "apt ", "yum ",
                                   "cat ", "ls ", "grep ", "find ", "echo ",
                                   "cd ", "pwd ", "mkdir ", "touch ",
                                   "cp ", "mv ", "tar ", "unzip ", "zip ")):
            return 1

        return 0

    def classify_command(self, command: str) -> Tuple[int, str]:
        """Classify a command into risk level and return (level, reason)."""
        stripped = command.strip()
        if not stripped:
            return (0, "empty")

        for pattern in self._DANGEROUS_PATTERNS_COMPILED:
            if pattern.search(stripped):
                return (3, f"dangerous operations: {stripped[:60]}")

        for cmd, pattern in self._MEDIUM_PATTERNS_COMPILED:
            if pattern.match(stripped):
                return (2, f"system modification: {cmd}")

        for cmd, pattern in self._LOW_PATTERNS_COMPILED:
            if pattern.match(stripped):
                return (1, f"file/system operation: {cmd}")

        for _cmd, pattern in self._SAFE_PATTERNS_COMPILED:
            if pattern.match(stripped):
                return (0, "safe operation")

        return (2, f"unknown command type: {stripped[:60]}")

    _DANGEROUS_PATTERNS_COMPILED = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]
    _MEDIUM_PATTERNS_COMPILED = [
        (cmd, re.compile(p, re.IGNORECASE)) for cmd, p in _MEDIUM_PATTERNS
    ]
    _LOW_PATTERNS_COMPILED = [
        (cmd, re.compile(p, re.IGNORECASE)) for cmd, p in _LOW_PATTERNS
    ]
    _SAFE_PATTERNS_COMPILED = [
        (cmd, re.compile(p, re.IGNORECASE)) for cmd, p in _SAFE_PATTERNS
    ]

    def _make_cache_key(self, text: str, mode: str) -> str:
        """Generate cache key from text and mode."""
        combined = f"{mode}:{text}"
        return hashlib.md5(combined.encode()).hexdigest()

    def _cache_result(self, key: str, complexity: float, meta: dict) -> None:
        """Cache classification result."""
        if len(self._cache) >= self._CACHE_MAX:
            oldest = min(self._cache, key=lambda k: self._cache[k][2])
            del self._cache[oldest]
        self._cache[key] = (complexity, meta, time.time())


_global_classifier: Optional[IntentClassifier] = None


def get_classifier(llm_provider: Any = None) -> IntentClassifier:
    """Get global intent classifier instance."""
    global _global_classifier
    if _global_classifier is None or llm_provider is not None:
        _global_classifier = IntentClassifier(llm_provider)
    return _global_classifier