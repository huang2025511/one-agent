"""Skill system — combines OpenClaw's MCP skills + OpenSquilla's MetaSkill.

A *Skill* is either:
    * a plain Markdown file (SKILL.md) describing a procedure, or
    * a MetaSkill (skill.yaml + markdown) that composes atomic skills into
      repeatable multi-step workflows, or
    * an MCP server (Model Context Protocol) where tools are dynamic.

The SkillManager exposes them uniformly as tools consumable by the LLM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.events import Event
from core.exceptions import InputValidationError  # noqa: F401
from core.plugin import Plugin
from memory.knowledge_graph import make_graph_search_handler  # noqa: F401
from multimodal import make_image_handler, make_transcribe_handler
from skills.document_search import make_doc_search_handler

from .document_search import DocumentStore
from .updater import make_updater_handler
from .wechat_login import make_wechat_login_handler  # noqa: F401

logger = logging.getLogger(__name__)

_DDG_RESULT_PATTERN = re.compile(
    r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<span[^>]*class="[^"]*snippet[^"]*"[^>]*>(.*?)</span>',
    re.DOTALL | re.IGNORECASE,
)
_DDG_LINK_PATTERN = re.compile(
    r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.DOTALL,
)
_BING_ALGO_PATTERN = re.compile(
    r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
    re.DOTALL | re.IGNORECASE,
)

# Lazy singleton — opened on first use to avoid SQLite I/O at import time.
_doc_store: Optional[DocumentStore] = None


def get_doc_store() -> DocumentStore:
    """Return the shared DocumentStore, creating it on first call.

    Replaces the old module-level ``_doc_store = DocumentStore()`` which
    opened a SQLite connection + ran schema migration on every ``import skills``.
    """
    global _doc_store
    if _doc_store is None:
        _doc_store = DocumentStore()
    return _doc_store


__all__ = [
    "Skill",
    "SkillManager",
    "get_doc_store",
]


class Skill:
    """Single-responsibility skill wrapper.

    The LLM sees it as a JSON-schema tool; the runtime dispatches to
    Python callables or shell commands declared in the skill's header.
    """

    def __init__(
        self,
        id: str,
        title: str,
        description: str,
        schema: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], Any],
        directory: Optional[str] = None,
        version: str = "1.0.0",
        changelog: Optional[List[str]] = None,
        hidden: bool = False,
    ) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.schema = schema
        self.handler = handler
        self.directory = directory
        self.version = version  # Semantic version (e.g., "1.2.3")
        self.changelog = changelog or []  # List of version notes
        # hidden=True 的技能不会出现在 /api/skills 列表和 LLM 工具清单中，
        # 但仍可通过 /skill <id> 直接调用（向后兼容）。用于隐藏已弃用/内部技能。
        self.hidden = hidden
        self.uses = 0
        self.last_used: Optional[float] = None
        # 性能优化：预计算 title+description 的小写形式, pick_relevant 每轮调用多次
        # 之前每次 pick_relevant 都 f"{title} {description}".lower() 重新分配字符串
        # 现在 register 时一次性算好, pick_relevant 直接读字段
        self._hay_lower: str = f"{title} {description}".lower()

    async def run(self, args: Dict[str, Any]) -> str:
        self.uses += 1
        self.last_used = time.time()
        try:
            # Validate args before execution
            self._validate_args(args)
            return await self.handler(args)
        except (ValueError, KeyError, TypeError, RuntimeError, asyncio.TimeoutError) as exc:
            logger.error("skill %s execution failed: %s", self.id, exc, exc_info=True)
            return f"[skill:{self.id} error] {exc}"
        except InputValidationError as exc:
            logger.warning("skill %s input validation failed: %s", self.id, exc)
            return f"[skill:{self.id} validation error] {exc}"
        except Exception as exc:
            logger.error("skill %s execution failed with unexpected error: %s", self.id, exc, exc_info=True)
            raise

    def _validate_args(self, args: Dict[str, Any]) -> None:
        """Validate skill arguments against schema."""
        if not isinstance(args, dict):
            raise InputValidationError("Arguments must be a dictionary")

        # Check required parameters
        required = self.schema.get("function", {}).get("parameters", {}).get("required", [])
        for param in required:
            if param not in args or args[param] is None:
                raise InputValidationError(f"Missing required parameter: {param}")

        # Validate parameters against schema properties
        properties = self.schema.get("function", {}).get("parameters", {}).get("properties", {})
        for key, value in args.items():
            if key not in properties:
                continue
            prop = properties[key]
            expected_type = prop.get("type")

            if expected_type == "string":
                if not isinstance(value, str):
                    raise InputValidationError(f"Parameter '{key}' must be a string")
                if len(value) > 10000:
                    raise InputValidationError(f"Parameter '{key}' too long (max 10000 characters)")
                # Validate enum if present
                if "enum" in prop and value not in prop["enum"]:
                    raise InputValidationError(f"Parameter '{key}' must be one of {prop['enum']}")
            elif expected_type == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise InputValidationError(f"Parameter '{key}' must be an integer")
                if "minimum" in prop and value < prop["minimum"]:
                    raise InputValidationError(f"Parameter '{key}' must be >= {prop['minimum']}")
                if "maximum" in prop and value > prop["maximum"]:
                    raise InputValidationError(f"Parameter '{key}' must be <= {prop['maximum']}")
            elif expected_type == "number":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise InputValidationError(f"Parameter '{key}' must be a number")
            elif expected_type == "boolean":
                if not isinstance(value, bool):
                    raise InputValidationError(f"Parameter '{key}' must be a boolean")
            elif expected_type == "array":
                if not isinstance(value, list):
                    raise InputValidationError(f"Parameter '{key}' must be an array")
                if "maxItems" in prop and len(value) > prop["maxItems"]:
                    raise InputValidationError(f"Parameter '{key}' has too many items (max {prop['maxItems']})")


class SkillManager(Plugin):
    """Loads & dispatches skills.  Also acts as a plugin on the event bus."""

    name = "skills"

    def __init__(self) -> None:
        super().__init__()
        self._skills: Dict[str, Skill] = {}
        self._builtin_dir: Optional[str] = None
        self._user_dir: Optional[str] = None
        self._community_dir: Optional[str] = None
        self._marketplace_dir: Optional[str] = None
        self._procedural_dir: Optional[str] = None
        self._mcp_servers: List[Dict[str, Any]] = []
        self._max_loaded_per_turn = 6
        self._system_executor = None

    # -------------------------------------------------------- lifecycle
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("skills", {}) or {}
        data_dir = ctx.config.get("agent", {}).get("data_dir", "./data")
        self._builtin_dir = cfg.get("builtin_skills_dir") or os.path.join(data_dir, "skills/builtin")
        self._user_dir = cfg.get("user_skills_dir") or os.path.join(data_dir, "skills/user")
        self._community_dir = cfg.get("community_skills_dir") or os.path.join(data_dir, "skills/community")
        self._marketplace_dir = cfg.get("marketplace_skills_dir") or os.path.join(data_dir, "skills/marketplace")
        # procedural 记忆目录：MemoryPlugin 自动学习的技能写到这里，
        # SkillManager 必须扫这个目录才能让学到的技能真正被注册和调度。
        self._procedural_dir = os.path.join(data_dir, "memory/skills")
        for d in (self._builtin_dir, self._user_dir, self._community_dir,
                  self._marketplace_dir, self._procedural_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        self._seed_builtins()
        self._scan_directory(self._builtin_dir)
        self._scan_directory(self._user_dir)
        self._scan_directory(self._community_dir)
        self._scan_directory(self._marketplace_dir)
        self._scan_directory(self._procedural_dir)
        # MCP server list — declared in config; started lazily
        self._mcp_servers = cfg.get("mcp_servers", []) or []
        self._max_loaded_per_turn = cfg.get("max_skills_per_turn", self._max_loaded_per_turn)
        # 保存 ctx 引用供 settings 技能使用
        self._ctx_ref = ctx
        self.bus.subscribe("cron", self._on_cron)
        logger.info("skills loaded: %d", len(self._skills))

    # ---------------------------------------------------------- public
    def all_skill_ids(self) -> List[str]:
        return list(self._skills.keys())

    def visible_skill_ids(self) -> List[str]:
        """返回非隐藏技能的 ID 列表（用于 /api/skills 和 LLM 工具清单）。"""
        return [sid for sid, sk in self._skills.items() if not sk.hidden]

    def get(self, id: str) -> Optional[Skill]:
        return self._skills.get(id)

    def pick_relevant(self, text: str, limit: int = 4) -> List[Skill]:
        """Simple keyword relevance — pick N skills whose title/description
        contain words from the user query.  This avoids loading the entire
        skill catalog into the LLM context.

        Matches: English words (3+ ASCII chars) and Chinese words (2+ CJK chars).
        Previously \\w{3,} alone matched Chinese characters as well (Python 3
        Unicode-aware \\w), missing all Chinese input and causing skill
        relevance to be essentially broken for Chinese-language users.
        """
        query_words = set()
        # English / ASCII alphanumeric words (3+ chars) — use [a-zA-Z0-9_]
        # instead of \\w to avoid matching Chinese/CJK characters
        for w in re.findall(r"[a-zA-Z0-9_]{3,}", text):
            query_words.add(w.lower())
        # Chinese character sequences (2+ chars)
        for w in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            query_words.add(w.lower())
        scored: List[tuple] = []
        for skill in self._skills.values():
            # 跳过隐藏技能：已弃用/内部技能不应暴露给 LLM
            if skill.hidden:
                continue
            # 性能优化：直接读预计算的 _hay_lower, 避免每轮重新拼接+lower
            hay = skill._hay_lower
            hits = sum(1 for w in query_words if w in hay)
            if hits > 0:
                scored.append((hits, skill.title, skill))
        scored.sort(reverse=True)
        return [s[2] for s in scored[:limit]]

    async def dispatch(self, skill_id: str, args: Dict[str, Any]) -> str:
        skill = self._skills.get(skill_id)
        if skill is None:
            return f"[unknown skill: {skill_id}]"
        try:
            result = await skill.run(args)
            try:
                self.bus.publish({"type": "skill_executed", "skill_id": skill_id, "status": "success"})
            except Exception:
                pass
            if skill_id == "python_execute":
                try:
                    self.bus.publish({"type": "python_executed", "status": "success"})
                except Exception:
                    pass
            return result
        except Exception as exc:
            try:
                self.bus.publish({"type": "skill_failed", "skill_id": skill_id, "error": str(exc)})
            except Exception:
                pass
            raise

    async def _on_cron(self, event: Event) -> None:
        """Handle skill_pattern_mining: re-scan skill directories for new skills."""
        job_name = event.get("name") or ""
        if job_name == "skill_pattern_mining":
            self._scan_directory(self._builtin_dir)
            self._scan_directory(self._user_dir)
            self._scan_directory(self._community_dir)
            self._scan_directory(self._marketplace_dir)
            # procedural 记忆目录也要重新扫描：MemoryPlugin 可能新增了自动学习的技能
            self._scan_directory(self._procedural_dir)
            logger.info("skill pattern mining: %d skills loaded", len(self._skills))

    def register(self, skill: Skill) -> None:
        self._skills[skill.id] = skill
        # 注册/更新技能后失效该技能的工具结果缓存，避免旧缓存
        # （如 system_run 的 30s TTL 缓存）返回过期结果。
        try:
            from core.tool_cache import get_tool_cache
            get_tool_cache().invalidate(skill.id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tool_cache invalidate for %s failed: %s", skill.id, exc)
        logger.info("registered skill: %s", skill.id)

    def unregister(self, skill_id: str) -> bool:
        """从运行时注册表中移除一个技能（不删除文件）。

        删除技能文件后调用此方法，使运行时立即生效而无需重启。
        返回 True 表示原来存在并已移除。
        """
        if skill_id in self._skills:
            del self._skills[skill_id]
            try:
                from core.tool_cache import get_tool_cache
                get_tool_cache().invalidate(skill_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("tool_cache invalidate for %s failed: %s", skill_id, exc)
            logger.info("unregistered skill: %s", skill_id)
            return True
        return False

    # --------------------------------------------------------- scan
    def _scan_directory(self, directory: str) -> None:
        root = Path(directory)
        if not root.exists():
            return
        for path in sorted(root.rglob("*.md")):
            try:
                body = path.read_text(encoding="utf-8")
                skill = self._parse_markdown_skill(path, body)
                if skill is not None:
                    self._skills[skill.id] = skill
            except (OSError, UnicodeDecodeError) as exc:
                logger.error("failed to load %s: %s", path, exc, exc_info=True)
                continue
            except Exception as exc:
                logger.error("failed to load %s with unexpected error: %s", path, exc, exc_info=True)
                continue

    def _parse_markdown_skill(self, path: Path, body: str) -> Optional[Skill]:
        # Expect a YAML front-matter block at the top:
        # ---
        # id: my-skill
        # title: Human title
        # description: what it does
        # command: shell-command {input}
        # ---
        # body (shown to the LLM as a reference)
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", body, re.DOTALL)
        if not m:
            return None
        import yaml
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            return None
        skill_id = str(meta.get("id") or path.stem)
        title = str(meta.get("title") or skill_id)
        description = str(meta.get("description") or m.group(2).strip()[:200])
        command = meta.get("command")
        schema = {
            "type": "function",
            "function": {
                "name": skill_id,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {"input": {"type": "string", "description": "user input"}},
                    "required": ["input"],
                },
            },
        }

        async def handler(args: Dict[str, Any]) -> str:
            input_text = args.get("input", "")
            if command:
                # shlex.quote 把用户输入转义为安全的单个参数
                import shlex
                safe_input = shlex.quote(input_text)
                resolved = command.replace("{input}", safe_input)
                try:
                    # 解析为列表参数，使用 asyncio.create_subprocess_exec 避免阻塞事件循环
                    cmd_parts = shlex.split(resolved, posix=True)
                    proc = await asyncio.create_subprocess_exec(
                        *cmd_parts,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                    return (stdout.decode() or "") + (stderr.decode() or "")
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()  # 等待进程终止，避免僵尸进程
                    return "[timeout]"
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    return f"[command error: {exc}]"
            return f"(skill {title}) {m.group(2).strip()[:500]}\n\ninput: {input_text}"

        return Skill(skill_id, title, description, schema, handler, directory=str(path.parent))

    # --------------------------------------------------------- builtin seeds
    async def _get_system_executor(self):
        """Lazy-init shared SystemExecutor singleton."""
        if self._system_executor is not None:
            return self._system_executor
        from executors.system import SystemExecutor
        executor = SystemExecutor()
        ctx = getattr(self, "_ctx_ref", None)
        if ctx is not None:
            await executor.setup(ctx)
        else:
            executor._enabled = True
            from executors.system import PasswordManager
            executor._pwd_manager = PasswordManager("", 60, 3, 5)
            executor._timeout_seconds = 30
            executor._workdir = "."
            executor._max_output_bytes = 65536
        self._system_executor = executor
        return self._system_executor

    def _seed_core_skills(self) -> None:
        import datetime

        async def echo_handler(args: Dict[str, Any]) -> str:
            return args.get("input", "")
        self.register(Skill(
            id="echo", title="Echo",
            description="Echo input back to the caller — used as a debug placeholder.",
            schema=_schema("echo", "echo input back", ["input"]),
            handler=echo_handler,
        ))

        async def help_handler(args: Dict[str, Any]) -> str:
            """显示帮助信息，列出所有可用命令。"""
            commands = [
                ("📋 /help /帮助", "显示此帮助信息"),
                ("📊 /status /状态", "显示系统状态（模型、网关等）"),
                ("ℹ️ /version /版本", "显示版本信息"),
                ("🧰 /skills /技能", "列出所有可用技能"),
                ("🌐 /gateways /网关", "显示所有网关状态"),
                ("📜 /history /历史", "显示最近的对话历史"),
                ("🧹 /clear /清屏", "清空当前对话上下文"),
                ("⚙️ /settings /设置", "查看和修改设置"),
                ("📡 /添加模型", "拉取并添加任意 provider 模型到智能路由"),
                ("📡 /显示模型", "显示所有层级模型及能力"),
                ("🔄 /update /更新", "从 Gitee 更新到最新版本"),
                ("♻️ /restart /重启", "重启 One-Agent"),
                ("💬 /wechat /微信", "启动微信网关并显示登录二维码"),
                ("🔍 /search /搜索", "搜索互联网（输入 /search 关键词）"),
                ("🎤 /transcribe /转文字", "语音转文字（输入 /transcribe 路径）"),
                ("🖼️ /image /图片", "图片描述（输入 /image 路径）"),
                ("📄 /doc /文档", "搜索已上传的文档"),
                ("🐍 /py /python", "执行 Python 代码（输入 /py 代码）"),
                ("💻 /shell /sh /命令", "执行系统命令（/shell ls -la [--password xxx]）"),
                ("🔓 /unlock /解锁", "解锁会话（/unlock 密码，60分钟有效）"),
                ("🔒 /lock /锁定", "撤销密码授权"),
                ("🔢 /calc /计算", "执行数学计算，如 /calc 2+2"),
                ("📝 /note /笔记", "保存笔记到文件"),
                ("⏰ /time /时间", "显示当前时间"),
                ("🚪 /quit /退出", "退出程序"),
                ("", "--- Round 6: 智能增强 ---"),
                ("🔁 /retry /重试", "重新生成上一条回复（换个说法）"),
                ("🔬 /deep /深度研究", "深度研究模式：分解子问题+多源搜索+综合（/deep 主题）"),
                ("📦 /batch /批量", "批量并行处理多个任务（/batch 任务1|任务2|任务3）"),
                ("⚖️ /compare /对比", "多模型对比：同时调用多个模型对比回答（/compare 问题）"),
                ("📊 /eval /评估", "评估回复质量（自动评分+改进建议）"),
                ("", "--- Round 7: 工具生态 ---"),
                ("📧 /email /邮件", "邮件管理：/email read|send|search|reply"),
                ("📅 /calendar /日历", "日程管理：/calendar list|add|delete [时间] [内容]"),
                ("🗄️ /db /数据库", "数据库查询（只读安全模式）：/db query SQL"),
                ("🔌 /mcp", "MCP 服务器管理（Claude Desktop 兼容）"),
                ("🔗 /openapi /api", "OpenAPI 接口调用：/openapi 路径"),
                ("🤖 /mesh /多智能体", "多智能体协作：/mesh 任务描述"),
                ("🔄 /workflow /流程", "工作流引擎：/ workflow run|list|show 名称"),
                ("📈 /chart /图表", "生成 Mermaid/ASCII 图表：/chart 类型 数据"),
                ("🌿 /branch /分支", "会话分支管理：/branch new|show|delete 名称"),
                ("🔀 /branch_switch", "切换会话分支：/branch_switch 名称"),
                ("📋 /branch_list", "列出所有会话分支"),
            ]
            lines = ["可用命令列表：", ""]
            lines.extend([f"  {cmd}  -  {desc}" for cmd, desc in commands])
            lines.append("")
            lines.append("💡 也可以直接输入自然语言，无需斜杠，AI 会自动选择合适的工具处理。")
            return "\n".join(lines)
        self.register(Skill(
            id="help", title="帮助",
            description="/help 或 /帮助：显示帮助信息，列出所有可用命令",
            schema=_schema("help", "display help information", []),
            handler=help_handler,
        ))

        async def status_handler(args: Dict[str, Any]) -> str:
            """显示系统状态。"""
            import os
            import platform
            lines = ["📊 系统状态：", ""]
            lines.append(f"  🐍 Python: {platform.python_version()}")
            lines.append(f"  💻 系统: {platform.system()} {platform.release()}")
            lines.append(f"  📁 工作目录: {os.getcwd()}")
            try:
                cfg = self._ctx_ref.config if self._ctx_ref else {}
                if cfg:
                    llm = cfg.get("llm", {})
                    lines.append(f"  🧠 主模型: {llm.get('primary_provider', '?')}/{llm.get('primary_model', '?')}")
                    lines.append(f"  🪶 轻量模型: {llm.get('lightweight_model', '?')}")
            except Exception as exc:
                logger.debug("ignored non-critical error: %s", exc)
            lines.append(f"  🧰 已加载技能: {len(self._skills)}")
            return "\n".join(lines)
        self.register(Skill(
            id="status", title="系统状态",
            description="/status 或 /状态：显示系统状态信息（模型、网关、Python 版本等）",
            schema=_schema("status", "show system status", []),
            handler=status_handler,
        ))

        async def version_handler(args: Dict[str, Any]) -> str:
            """显示版本信息。"""
            import platform
            try:
                from one_agent import __version__ as VERSION
            except (ImportError, AttributeError):
                VERSION = "2.0.0"
            return (
                f"🤖 One-Agent 版本信息：\n"
                f"  版本: {VERSION}\n"
                f"  Python: {platform.python_version()}\n"
                f"  平台: {platform.platform()}"
            )
        self.register(Skill(
            id="version", title="版本信息",
            description="/version 或 /版本：显示 One-Agent 版本信息",
            schema=_schema("version", "show version info", []),
            handler=version_handler,
        ))

        async def list_skills_handler(args: Dict[str, Any]) -> str:
            """列出所有已注册的技能。"""
            lines = [f"🧰 已注册技能 (共 {len(self._skills)} 个)：", ""]
            for i, (sid, skill) in enumerate(sorted(self._skills.items()), 1):
                title = skill.title if hasattr(skill, 'title') else sid
                lines.append(f"  {i:3d}. {sid:20s} - {title}")
            return "\n".join(lines)
        self.register(Skill(
            id="list_skills", title="技能列表",
            description="/skills 或 /技能：列出所有可用的技能",
            schema=_schema("list_skills", "list all skills", []),
            handler=list_skills_handler,
        ))

        async def list_gateways_handler(args: Dict[str, Any]) -> str:
            """列出所有网关状态。"""
            try:
                cfg = self._ctx_ref.config if self._ctx_ref else {}
                gateways = cfg.get("gateways", {}) or {}
                if not gateways:
                    return "ℹ️ 当前没有配置任何网关。\n使用 /settings 配置网关。"
                lines = ["🌐 网关状态：", ""]
                for name, gcfg in gateways.items():
                    enabled = gcfg.get("enabled", False) if isinstance(gcfg, dict) else False
                    icon = "✅" if enabled else "⏸️"
                    status = "已启用" if enabled else "未启用"
                    lines.append(f"  {icon} {name:15s} - {status}")
                return "\n".join(lines)
            except Exception as exc:
                return f"❌ 获取网关状态失败: {exc}"
        self.register(Skill(
            id="list_gateways", title="网关列表",
            description="/gateways 或 /网关：列出所有网关及其状态",
            schema=_schema("list_gateways", "list gateways", []),
            handler=list_gateways_handler,
        ))

        async def history_handler(args: Dict[str, Any]) -> str:
            """显示最近的对话历史。"""
            try:
                sid = args.get("session_id", "default")
                if not self._ctx_ref:
                    return "❌ 无法访问会话存储"
                from memory.session_store import SessionStore
                store = SessionStore(self._ctx_ref.config.get("agent", {}).get("data_dir", "./data") + "/memory/sessions.db")
                session = store.get_session(sid)
                if not session:
                    return "📜 当前会话暂无历史记录"
                messages = session.get("messages", []) if isinstance(session, dict) else []
                if not messages:
                    return "📜 当前会话暂无历史记录"
                lines = [f"📜 最近 {len(messages)} 条对话 (session: {sid})：", ""]
                for msg in messages[-10:]:
                    role = msg.get("role", "?")
                    content = msg.get("content", "")[:80]
                    icon = "👤" if role == "user" else "🤖"
                    lines.append(f"  {icon} [{role}] {content}")
                return "\n".join(lines)
            except Exception as exc:
                return f"❌ 获取历史失败: {exc}"
        self.register(Skill(
            id="history", title="对话历史",
            description="/history 或 /历史：显示最近的对话历史",
            schema=_schema("history", "show conversation history", []),
            handler=history_handler,
        ))

        async def clear_handler(args: Dict[str, Any]) -> str:
            """清空当前会话。"""
            try:
                sid = args.get("session_id", "default")
                if not self._ctx_ref:
                    return "❌ 无法访问会话存储"
                from memory.session_store import SessionStore
                store = SessionStore(self._ctx_ref.config.get("agent", {}).get("data_dir", "./data") + "/memory/sessions.db")
                store.delete_session(sid)
                return "✅ 已清空当前对话历史"
            except Exception as exc:
                return f"❌ 清空失败: {exc}"
        self.register(Skill(
            id="clear", title="清屏",
            description="/clear 或 /清屏：清空当前对话历史",
            schema=_schema("clear", "clear conversation", []),
            handler=clear_handler,
        ))

        async def restart_handler(args: Dict[str, Any]) -> str:
            """重启 One-Agent。

            写入重启标记后延迟 1.5 秒执行 execv，
            确保 turn_completed 事件有机会发送给用户。
            新进程启动时读取标记，感知到刚发生过重启。

            安全修复：在 os.execv 之前调用所有 plugin 的 stop()，
            确保 SQLite commit、httpx 连接池关闭、cost tracker 落盘等
            清理逻辑执行。原实现直接 os.execv 跳过 finally 块，导致
            内存中数据丢失（如 cost tracker 未落盘、session 未持久化）。
            """
            # 防止重复触发：如果已有 restart task 在运行，直接返回
            existing = getattr(self, "_restart_task", None)
            if existing is not None and not existing.done():
                return "♻️ 重启已在进行中，请稍候..."

            import os
            import sys
            import json
            import time as _time
            from pathlib import Path

            data_dir = os.environ.get("ONE_AGENT_DATA_DIR", "./data")
            marker = Path(data_dir) / "restart_marker.json"

            # 写入重启标记，供新进程启动时读取
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({
                    "timestamp": _time.time(),
                    "message": "重启完成，新版本已生效",
                }, ensure_ascii=False), encoding="utf-8")
            except Exception as exc:
                logger.debug("ignored non-critical error: %s", exc)

            # 延迟重启，让事件总线把回复发给用户。
            # 关键：_do_restart 是 async，先 sleep 1.5s 让回复发出，
            # 再 await 所有 plugin 的 stop()（带 3s timeout 避免卡死），
            # 最后 os.execv。
            async def _do_restart_async() -> None:
                import asyncio as _aio
                # 1) 让事件总线把回复发给用户
                try:
                    await _aio.sleep(1.5)
                except _aio.CancelledError:
                    pass
                logger.info("restart: begin graceful shutdown")
                # 2) 优雅停止所有 plugin
                ctx_ref = getattr(self, "_ctx_ref", None)
                if ctx_ref is not None:
                    plugins = list(getattr(ctx_ref, "_plugins", []) or [])
                    # 反向停止（依赖项最后停）
                    for plugin in reversed(plugins):
                        try:
                            stop_fn = getattr(plugin, "stop", None)
                            if stop_fn is None:
                                continue
                            maybe_coro = stop_fn()
                            if hasattr(maybe_coro, "__await__"):
                                try:
                                    await _aio.wait_for(maybe_coro, timeout=3.0)
                                except _aio.TimeoutError:
                                    logger.warning(
                                        "restart: plugin %s stop timeout, forcing restart",
                                        getattr(plugin, "name", "?"),
                                    )
                                except Exception:
                                    logger.exception(
                                        "restart: plugin %s stop failed",
                                        getattr(plugin, "name", "?"),
                                    )
                        except Exception:
                            logger.exception(
                                "restart: plugin %s stop raised",
                                getattr(plugin, "name", "?"),
                            )
                # 3) 清理后替换进程
                logger.info("restart: all plugins stopped, exec-ing new process")
                try:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception:
                    logger.error("restart: os.execv failed — process will keep running with old code")
                    logger.exception("restart: os.execv exception")

            import asyncio as _aio
            try:
                loop = _aio.get_running_loop()
                # 关键修复：保存 task 强引用，避免被 GC。
                # asyncio 文档明确说 event loop 只保留 weak reference，
                # task 在执行前可能被 GC 回收，导致重启静默不执行
                # （用户看到"正在重启"消息后回到提示符，但什么都没发生）。
                # 把 task 挂到 self._restart_task 形成强引用。
                self._restart_task = loop.create_task(_do_restart_async())
                # 加 done_callback 记录异常（task 因异常结束时 logging）
                def _log_restart_failure(task):
                    if task.cancelled():
                        logger.warning("restart: task was cancelled, restart may not have completed")
                    elif task.exception():
                        logger.error("restart: task failed: %s", task.exception(), exc_info=task.exception())
                self._restart_task.add_done_callback(_log_restart_failure)
            except RuntimeError:
                # 没 running loop，直接同步执行（跳过清理作为最后兜底）
                logger.info("restart: no running loop, sync os.execv")
                try:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception:
                    logger.exception("restart: fallback os.execv failed")

            return "♻️ 正在重启 One-Agent...\n重启后将自动加载最新版本，请稍候。"
        self.register(Skill(
            id="restart", title="重启",
            description="/restart 或 /重启：重启 One-Agent 程序",
            schema=_schema("restart", "restart One-Agent", []),
            handler=restart_handler,
        ))

        async def now_handler(args: Dict[str, Any]) -> str:
            return datetime.datetime.now().isoformat()
        self.register(Skill(
            id="now", title="Current time",
            description="显示当前时间。使用方式: /time 或 /时间",
            schema=_schema("now", "return current timestamp", []),
            handler=now_handler,
        ))

        async def calc_handler(args: Dict[str, Any]) -> str:
            import ast
            import operator
            # LLM 经常把算式参数命名为 expression / expr / query / formula 等，
            # 而 schema 只声明了 input。直接拒掉会让 LLM 多走一轮重试，
            # 浪费 token。这里接受常见别名，提升一次成功率。
            expr = ""
            for key in ("input", "expression", "expr", "formula", "query", "text"):
                v = args.get(key)
                if v:
                    expr = str(v).strip()
                    break
            if not expr:
                return "[calc: 缺少算式参数]"
            if not re.fullmatch(r"[0-9+\-*/(). ]+", expr):
                return "[invalid math expression]"
            _ops = {
                ast.Add: operator.add, ast.Sub: operator.sub,
                ast.Mult: operator.mul, ast.Div: operator.truediv,
                ast.Pow: operator.pow, ast.USub: operator.neg,
                ast.UAdd: operator.pos,
            }
            def _eval(node):
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                    return node.value
                # ast.Num 在 Python 3.8 已废弃、3.14 已删除 — 只保留 ast.Constant
                # 分支即可（py 3.8+ 的所有数字字面量都走 ast.Constant）。
                if isinstance(node, ast.BinOp):
                    return _ops[type(node.op)](_eval(node.left), _eval(node.right))
                if isinstance(node, ast.UnaryOp):
                    return _ops[type(node.op)](_eval(node.operand))
                raise ValueError(f"unsupported node: {type(node).__name__}")
            try:
                tree = ast.parse(expr, mode="eval")
                return str(_eval(tree.body))
            except (ValueError, TypeError, SyntaxError) as exc:
                logger.error("math expression evaluation failed: %s", exc, exc_info=True)
                return f"[math error: {exc}]"
        # calc 的 schema 不强制 required（calc_handler 自己兜底接受
        # input / expression / expr / formula / query / text 等别名）。
        # 之前 schema 把 input 设为 required，LLM 用 expression 调用会
        # 被 _validate_args 拦掉、handler 根本跑不到，浪费一轮重试。
        self.register(Skill(
            id="calc", title="Calculator",
            description="/calc 或 /计算：执行数学计算。参数名 input（字符串，算式）。"
                         "示例：input=\"2+2*3\"",
            schema=_schema("calc", "evaluate arithmetic expression; "
                                   "pass the expression as the 'input' parameter",
                           []),
            handler=calc_handler,
        ))

        async def save_note(args: Dict[str, Any]) -> str:
            # 跨平台文件锁：fcntl (Unix) 或 msvcrt (Windows)
            try:
                import fcntl
                _has_fcntl = True
                _has_msvcrt = False
            except ImportError:
                _has_fcntl = False
                try:
                    import msvcrt
                    _has_msvcrt = True
                except ImportError:
                    _has_msvcrt = False
            text = str(args.get("input", ""))
            target = Path(self._builtin_dir or "./data/skills/builtin") / "user_notes.log"
            target.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            with open(target, "a", encoding="utf-8") as f:
                if _has_fcntl:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                        f.write(f"[{ts}] {text}\n")
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                elif _has_msvcrt:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                        f.write(f"[{ts}] {text}\n")
                    finally:
                        try:
                            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
                else:
                    f.write(f"[{ts}] {text}\n")
            return "note saved"
        self.register(Skill(
            id="save_note", title="Save note",
            description="保存笔记到文件。使用方式: /note 今天天气真好",
            schema=_schema("save_note", "append persistent note", ["input"]),
            handler=save_note,
        ))

    def _seed_system_skills(self) -> None:
        # ---------- 系统命令执行技能 ----------
        async def system_run_handler(args: Dict[str, Any]) -> str:
            """执行系统命令。使用方式: /shell ls -la"""
            executor = await self._get_system_executor()
            command = str(args.get("command", "")).strip()

            if not command:
                return "用法: /shell <命令>\n示例:\n  /shell ls -la\n  /shell pip install requests"

            try:
                result = await executor.dispatch("system.run", {
                    "command": command,
                })
            except Exception as exc:
                return f"执行错误: {exc}"

            if not isinstance(result, dict):
                return str(result)

            ok = result.get("ok", False)
            err = result.get("error", "")
            level = result.get("risk_level", 0)
            label = result.get("risk_label", "UNKNOWN")

            if ok:
                stdout = result.get("stdout", "")
                stderr = result.get("stderr", "")
                parts = []
                if level > 0:
                    parts.append(f"✅ 已执行 (风险级别: {label})")
                else:
                    parts.append("✅ 已执行 (安全操作)")
                if stdout:
                    parts.append(stdout.strip())
                if stderr:
                    parts.append(f"警告/错误: {stderr.strip()}")
                return "\n".join(parts)

            return f"❌ 执行失败: {err}"

        self.register(Skill(
            id="system_run", title="系统命令",
            description="/shell 或 /sh：执行系统命令。所有命令直接执行，无需密码。",
            schema={
                "type": "function",
                "function": {
                    "name": "system_run",
                    "description": "执行系统命令。所有命令直接执行，无需密码验证。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "要执行的系统命令"},
                        },
                        "required": ["command"],
                    },
                },
            },
            handler=system_run_handler,
        ))

        # ---------- 解锁会话技能（已弃用） ----------
        async def system_unlock_handler(args: Dict[str, Any]) -> str:
            """已弃用：系统默认就是 OS 模式，无需密码解锁。"""
            return "ℹ️ /unlock 已弃用。\n\n系统默认就是 OS 模式，所有命令可直接执行，无需密码验证。"

        self.register(Skill(
            id="system_unlock", title="解锁系统（已弃用）",
            description="/unlock：已弃用，系统默认无需密码",
            schema={
                "type": "function",
                "function": {
                    "name": "system_unlock",
                    "description": "已弃用：系统默认无需密码解锁",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "password": {"type": "string", "description": "已弃用，可留空"},
                        },
                    },
                },
            },
            handler=system_unlock_handler,
            hidden=True,
        ))

        # ---------- 锁定会话技能（已弃用） ----------
        async def system_lock_handler(args: Dict[str, Any]) -> str:
            """已弃用：系统默认无密码锁，无需锁定。"""
            return "ℹ️ /lock 已弃用。\n\n系统默认无密码锁，无需锁定。"

        self.register(Skill(
            id="system_lock", title="锁定系统（已弃用）",
            description="/lock：已弃用，系统默认无密码锁",
            schema={
                "type": "function",
                "function": {
                    "name": "system_lock",
                    "description": "已弃用：系统默认无密码锁",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            handler=system_lock_handler,
            hidden=True,
        ))

        # ---------- 主动消息推送技能 ----------
        async def send_message_handler(args: Dict[str, Any]) -> str:
            """主动发送消息到当前对话的用户。使用方式: 作为工具被LLM调用

            当 Agent 需要主动通知用户任务完成、发送提醒、或推送消息时，
            调用此工具即可。消息会通过用户当前使用的网关（微信、Telegram等）发送。
            """
            text = str(args.get("text", "")).strip()
            chat_id = str(args.get("chat_id", "")).strip()
            gateway = str(args.get("gateway", "")).strip()

            if not text:
                return "错误: text 参数不能为空"

            # 从 contextvars 中获取当前 turn 的 session_id 来推断 chat_id 和 gateway
            if not chat_id:
                try:
                    from core.context import current_turn_var
                    current_turn = current_turn_var.get()
                    if current_turn is not None:
                        session_id = getattr(current_turn, "session_id", "")
                        # session_id 格式通常是 "gateway-chat_id"，比如 "wechat-xxx"
                        parts = session_id.split("-", 1)
                        if len(parts) == 2:
                            if not gateway:
                                # 从 session_id 前缀推断网关
                                gw_prefix = parts[0]
                                gateway_map = {
                                    "wechat": "wechat_personal",
                                    "telegram": "telegram",
                                    "wecom": "wecom",
                                    "feishu": "feishu",
                                    "dingtalk": "dingtalk",
                                    "discord": "discord",
                                    "slack": "slack",
                                }
                                gateway = gateway_map.get(gw_prefix, "")
                            if not chat_id:
                                chat_id = parts[1]
                except Exception as exc:
                    logger.debug("ignored non-critical error: %s", exc)

            if not chat_id:
                return "错误: 无法确定目标 chat_id，请显式指定 chat_id 参数"

            # 发布事件到事件总线，由对应网关处理发送
            ctx_ref = getattr(self, "_ctx_ref", None)
            if ctx_ref is not None and hasattr(ctx_ref, "bus"):
                try:
                    ctx_ref.bus.publish({
                        "type": "bot_send_message",
                        "chat_id": chat_id,
                        "text": text,
                        "gateway": gateway,
                        "source": "skill:send_message",
                    })
                    return f"✅ 消息已发送（chat_id: {chat_id[:8]}...)"
                except Exception as exc:
                    return f"发送失败: {exc}"

            return "发送失败: 事件总线不可用"

        self.register(Skill(
            id="send_message", title="主动发消息",
            description="主动向用户发送消息/通知/提醒，无需等待用户提问。"
                        "适用于：任务完成通知、定时提醒、异步结果推送、"
                        "主动告知进度、发送警示信息等场景。"
                        "调用此工具会立即通过当前对话渠道发送消息。",
            schema={
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "主动向用户发送消息（通知、提醒、任务完成等）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "要发送的消息内容",
                            },
                            "chat_id": {
                                "type": "string",
                                "description": "目标用户/群组ID（可选，默认发给当前对话者）",
                            },
                            "gateway": {
                                "type": "string",
                                "description": "目标网关（可选，如 wechat_personal、telegram 等）",
                            },
                        },
                        "required": ["text"],
                    },
                },
            },
            handler=send_message_handler,
        ))

    def _seed_lifecycle_skills(self) -> None:
        # ---------- 更新技能 ----------
        async def updater_handler(args: Dict[str, Any]) -> str:
            """更新 One-Agent 到最新版本。使用方式: /update 或 /更新"""
            updater = make_updater_handler()
            return await updater(args)
        self.register(Skill(
            id="updater", title="更新",
            description="/update 或 /更新：从 GitHub 更新 One-Agent 到最新版本，支持 Git 和 curl 两种更新方式",
            schema={
                "type": "function",
                "function": {
                    "name": "updater",
                    "description": "从 GitHub 更新 One-Agent 到最新版本，支持 Git 和 curl 两种更新方式",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "branch": {
                                "type": "string",
                                "description": "分支名称，默认 main",
                                "default": "main"
                            }
                        }
                    },
                },
            },
            handler=updater_handler,
        ))

        # ---------- 微信登录技能 ----------
        async def wechat_login_handler(args: Dict[str, Any]) -> str:
            """启动微信网关并显示登录二维码（按需启动）"""
            from skills.wechat_login import make_wechat_login_handler  # noqa: F811
            # Pass the SkillManager's ctx_ref so the handler can locate the
            # wechat gateway plugin without importing non-existent names.
            wechat_handler = make_wechat_login_handler(getattr(self, "_ctx_ref", None))
            return await wechat_handler(args)
        self.register(Skill(
            id="wechat_login", title="微信登录",
            description="/wechat 或 /微信：启动微信网关并显示登录二维码（按需启动）",
            schema={
                "type": "function",
                "function": {
                    "name": "wechat_login",
                    "description": "启动微信网关并显示登录二维码（按需启动）",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    },
                },
            },
            handler=wechat_login_handler,
        ))

        # ---------- 退出技能 ----------
        async def quit_handler(args: Dict[str, Any]) -> str:
            """退出 One-Agent。

            Use sys.exit(0) (raises SystemExit) instead of os._exit(0) so the
            main loop's finally block runs app.stop() — flushing logs,
            closing httpx clients, committing SQLite, and stopping plugins.
            os._exit skips all cleanup (atexit, finally, asyncio shutdown).
            """
            import sys
            results = ["正在退出...", "再见！下次见 👋"]
            # Defer the exit slightly so the reply can be surfaced first.
            loop = asyncio.get_running_loop()
            loop.call_later(0.1, sys.exit, 0)
            return "\n".join(results)
        self.register(Skill(
            id="quit", title="退出",
            description="/quit 或 /退出：退出 One-Agent 程序",
            schema={
                "type": "function",
                "function": {
                    "name": "quit",
                    "description": "退出 One-Agent 程序",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    },
                },
            },
            handler=quit_handler,
        ))

    def _seed_settings_skill(self) -> None:
        # ---------- 设置管理技能 ----------
        async def settings_handler(args: Dict[str, Any]) -> str:
            """通过自然语言读取或修改配置。

            支持的操作：
            - 读取: "查看模型" / "当前温度" / "show model"
            - 修改: "把模型改成GPT-4" / "设置温度为0.7" / "开启Docker"
            - 列出: "列出所有设置" / "show all settings"
            """
            input_text = str(args.get("input", "")).strip()
            if not input_text:
                return "请说明要查看或修改的设置，例如：'查看模型'、'把温度改为0.7'"

            # 获取运行时配置引用
            ctx_ref = getattr(self, "_ctx_ref", None)
            config = ctx_ref.config if ctx_ref else {}

            result = _process_settings_command(input_text, config, bus=self.bus)
            return result

        self.register(Skill(
            id="settings", title="Settings Manager",
            description="读取或修改 Agent 配置设置，包括模型、温度、网关开关、API Key、缓存等。"
                        "支持自然语言操作：查看模型、把温度改为0.7、设置默认模型、开启缓存、"
                        "关闭网关、配置API密钥、修改配置、调整参数、查看配置、查看设置、"
                        "temperature、model、cache、gateway、api_key、config、设置、配置、参数、调整",
            schema={
                "type": "function",
                "function": {
                    "name": "settings",
                    "description": "读取或修改 Agent 配置设置",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "input": {
                                "type": "string",
                                "description": "设置操作描述，如 '查看模型' 或 '把温度改为0.7'",
                            },
                        },
                        "required": ["input"],
                    },
                },
            },
            handler=settings_handler,
        ))

    def _seed_web_search_skill(self) -> None:
        # ---------- 网页搜索技能 ----------
        async def web_search_handler(args: Dict[str, Any]) -> str:
            """搜索互联网获取最新信息。多源自动切换。

            修复 (V65)：原 handler 只有 DuckDuckGo Lite + Bing 两个源，在当前 sandbox
            环境下 SSL 握手全部失败。加入 360 搜索（国内可访问、HTML 含真实结果）作为
            主路径，保留 DuckDuckGo/Bing 作为国外环境 fallback。

            V66：支持翻页参数 page（默认 1），Bing/360/DDG 都加翻页参数。
            不依赖任何 API key，纯 HTML 解析。任一源成功即返回结果。
            失败时返回明确诊断 + 建议用 system_run / python_execute 直接 curl 目标 URL。
            """
            query = str(args.get("input", "")).strip()
            if not query:
                return "[web_search error: empty query]"

            # 翻页支持：page=1（默认第一页），page=2 第二页，以此类推
            try:
                page = int(args.get("page", 1))
                if page < 1:
                    page = 1
            except (ValueError, TypeError):
                page = 1

            import html as _htmlmod
            import re as _re
            from urllib.parse import quote as _url_quote

            import httpx as _httpx

            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            results: list[str] = []
            sources_tried: list[str] = []
            source_errors: dict[str, str] = {}  # 每个源各自的错误信息
            succeeded_source: str = ""

            def _strip_html(s: str) -> str:
                """Strip HTML tags, decode entities, collapse whitespace."""
                s = _re.sub(r"<[^>]+>", " ", s)
                s = _htmlmod.unescape(s)  # &amp; → &  &#0183; → ·  &ensp; → space
                s = _re.sub(r"\s+", " ", s).strip()
                return s

            def _looks_like_real_link(href: str, title: str, query: str) -> bool:
                """过滤明显是导航/广告/登录的链接，要求标题与查询相关。"""
                if not href.startswith("http"):
                    return False
                # 静态资源扩展名（.js 后必须跟 ?/#/$，避免误杀 .json）
                if _re.search(
                    r'\.(css|js|png|jpe?g|gif|svg|ico|woff2?|bmp|tiff?)(\?|#|$)',
                    href.lower(),
                ):
                    return False
                skip_patterns = [
                    "javascript:", "mailto:", "#", "/login", "/reg",
                ]
                for sp in skip_patterns:
                    if sp in href.lower():
                        return False
                # 跳过本搜索域自身的导航/跳转链接
                for own in ("so.com/link", "ai.so.com/search", "sogou.com/link",
                            "baidu.com/link", "bing.com/ck/", "bing.com/search?",
                            "duckduckgo.com/?", "duckduckgo.com/html",
                            "r.bing.com", "go.microsoft.com"):
                    if own in href.lower():
                        return False
                # V67 P3-3：title 长度阈值放宽到 3（"API"/"Go" 等英文短标题放行）
                if not title or len(title) < 3:
                    return False
                # query 关键词至少有一个在 title 中（不区分大小写）
                q_words = [w for w in _re.split(r"[\s,]+", query) if len(w) >= 2]
                if not q_words:
                    return True
                title_lower = title.lower()
                return any(w.lower() in title_lower for w in q_words)

            def _extract_bing_block(block: str, seen_urls: set[str] | None = None) -> str | None:
                """Bing CN 专用：优先从 <h2><a> 提取标题+直链，摘要用 b_lineclamp/b_caption。

                Bing 的 b_algo 块结构：
                  <div class="b_tpcn"><a class="tilk" href="真实URL">...网站图标+cite...</a></div>
                  <h2><a href="真实URL">真正标题</a></h2>
                  <div class="b_caption"><p class="b_lineclamp2">摘要</p></div>

                旧 pattern 先匹配到 b_tpcn 里的 a.tilk，把 cite 文本当成了标题。
                """
                # 优先 h2 > a（真正的标题链接）
                h2_m = _re.search(
                    r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                    block, _re.DOTALL,
                )
                if h2_m:
                    url_r = h2_m.group(1)
                    title = _strip_html(h2_m.group(2))
                else:
                    # fallback: 任意 a 标签（跳过 tilk 图标链接）
                    link_m = _re.search(
                        r'<a(?![^>]*class="[^"]*tilk)[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                        block, _re.DOTALL,
                    )
                    if not link_m:
                        return None
                    url_r = link_m.group(1)
                    title = _strip_html(link_m.group(2))
                # 去重：同一 URL 不重复加入
                if seen_urls is not None:
                    if url_r in seen_urls:
                        return None
                    seen_urls.add(url_r)
                # 摘要：b_lineclamp* 优先，其次 b_caption 内 p
                snippet = ""
                snip_m = _re.search(
                    r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>',
                    block, _re.DOTALL | _re.IGNORECASE,
                )
                if snip_m:
                    snippet = _strip_html(snip_m.group(1))
                if not snippet:
                    cap_m = _re.search(
                        r'<div[^>]*class="[^"]*b_caption[^"]*"[^>]*>(.*?)</div>',
                        block, _re.DOTALL | _re.IGNORECASE,
                    )
                    if cap_m:
                        p_m = _re.search(r"<p[^>]*>(.*?)</p>", cap_m.group(1), _re.DOTALL)
                        if p_m:
                            snippet = _strip_html(p_m.group(1))
                if not _looks_like_real_link(url_r, title, query):
                    return None
                line = title
                if snippet:
                    line += "\n  " + snippet[:200]
                line += "\n  " + url_r
                return line

            def _parse_result_blocks(
                html_text: str,
                patterns: list[str],
                seen_urls: set[str] | None = None,
            ) -> list[str]:
                """通用 HTML 块解析（360/DDG 用），Bing 用 _extract_bing_block。

                V67 P2-7：seen_urls 由调用方传入，跨源共享去重。
                """
                blocks = []
                if seen_urls is None:
                    seen_urls = set()  # 兼容旧调用
                for pat in patterns:
                    for m in _re.finditer(pat, html_text, _re.DOTALL | _re.IGNORECASE):
                        block = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                        link_m = _re.search(
                            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
                            block, _re.DOTALL,
                        )
                        if not link_m:
                            continue
                        url_r = link_m.group(1)
                        if url_r in seen_urls:
                            continue
                        title = _strip_html(link_m.group(2))
                        snippet = ""
                        snip_m = _re.search(
                            r'<p[^>]*class="[^"]*(?:desc|snippet|content|abstract|res-desc)[^"]*"[^>]*>(.*?)</p>',
                            block, _re.DOTALL | _re.IGNORECASE,
                        )
                        if snip_m:
                            snippet = _strip_html(snip_m.group(1))
                        if not snippet:
                            any_p = _re.findall(r"<p[^>]*>(.*?)</p>", block, _re.DOTALL)
                            for p in any_p:
                                cleaned = _strip_html(p)
                                if len(cleaned) > 30:
                                    snippet = cleaned
                                    break
                        if _looks_like_real_link(url_r, title, query):
                            seen_urls.add(url_r)
                            line = title
                            if snippet:
                                line += "\n  " + snippet[:200]
                            line += "\n  " + url_r
                            blocks.append(line)
                return blocks

            async def _try_bing() -> bool:
                """Bing CN — 国内可访问、返回直链（非跳转URL）、标题准确、摘要完整。

                V65 实测：cn.bing.com HTTP 200, 100KB, 10 个 b_algo 块，
                每个 h2>a 含真实目标 URL（如 agnes-ai.com、zhihu.com）。
                V67 P1-3：try 移到 for 循环内部，单 host 异常不中断下一个 host。
                V67 P2-7：bing_seen_urls 用 handler 级 seen_urls 跨源去重。
                """
                nonlocal results
                bing_err = ""
                for host in ("https://cn.bing.com/search", "https://www.bing.com/search"):
                    try:
                        bing_url = host + "?q=" + _url_quote(query) + "&setlang=zh-cn"
                        if page > 1:
                            bing_url += "&first=" + str((page - 1) * 10 + 1)
                        async with _httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                            resp = await client.get(bing_url, headers=headers)
                        if resp.status_code != 200:
                            bing_err = "HTTP " + str(resp.status_code)
                            continue
                        # 提取 b_algo 块，用专用 Bing 解析器
                        algo_blocks = _re.findall(
                            r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
                            resp.text, _re.DOTALL | _re.IGNORECASE,
                        )
                        parsed = []
                        for blk in algo_blocks:
                            # V67 P2-7：用 handler 级 seen_urls 跨源去重
                            line = _extract_bing_block(blk, seen_urls)
                            if line:
                                parsed.append(line)
                        if parsed:
                            results.extend(parsed[:6])
                            return True
                        bing_err = "0 results"
                    except (_httpx.HTTPStatusError, _httpx.RequestError, _httpx.TimeoutException) as e:
                        bing_err = type(e).__name__ + ": " + str(e)[:100]
                        continue  # V67 P1-3：单 host 失败继续尝试下一个 host
                source_errors["Bing"] = bing_err or "0 results"
                return False

            async def _try_360() -> bool:
                """360 搜索 — 国内 fallback。注意：URL 是 ai.so.com/search 跳转包装。"""
                nonlocal results
                try:
                    url = "https://www.so.com/s?q=" + _url_quote(query)
                    if page > 1:
                        url += "&pn=" + str((page - 1) * 10 + 1)
                    async with _httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                        resp = await client.get(url, headers=headers)
                    if resp.status_code != 200:
                        source_errors["360搜索"] = "HTTP " + str(resp.status_code)
                        return False
                    patterns = [
                        r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>(.*?)</h3>',
                        r'<a[^>]+class="[^"]*res-title[^"]*"[^>]*>(.*?)</a>',
                        r'<h3[^>]*>(.*?)</h3>',
                    ]
                    # V67 P2-7：_parse_result_blocks 传入 handler 级 seen_urls 跨源去重
                    blocks = _parse_result_blocks(resp.text, patterns, seen_urls)
                    if blocks:
                        results.extend(blocks[:6])
                        return True
                    source_errors["360搜索"] = "0 results"
                    return False
                except (_httpx.HTTPStatusError, _httpx.RequestError, _httpx.TimeoutException) as e:
                    source_errors["360搜索"] = type(e).__name__ + ": " + str(e)[:100]
                    return False

            async def _try_ddg() -> bool:
                """DuckDuckGo Lite — 国外环境 fallback。"""
                nonlocal results
                try:
                    async with _httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                        post_data = {"q": query, "kl": "cn-zh"}
                        if page > 1:
                            post_data["s"] = str((page - 1) * 20)  # DDG Lite 用 s=offset
                        resp = await client.post(
                            "https://lite.duckduckgo.com/lite/",
                            data=post_data,
                            headers=headers,
                        )
                    if resp.status_code != 200:
                        source_errors["DuckDuckGo"] = "HTTP " + str(resp.status_code)
                        return False
                    patterns = [
                        r'<td[^>]*class="[^"]*result-link[^"]*"[^>]*>(.*?)</td>',
                        r'<a[^>]*class="[^"]*result-link[^"]*"[^>]*>(.*?)</a>',
                    ]
                    # V67 P2-7：_parse_result_blocks 传入 handler 级 seen_urls 跨源去重
                    blocks = _parse_result_blocks(resp.text, patterns, seen_urls)
                    if not blocks:
                        for m in _re.finditer(
                            r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                            resp.text, _re.DOTALL,
                        ):
                            url_r = m.group(1)
                            title = _strip_html(m.group(2))
                            if _looks_like_real_link(url_r, title, query) and url_r not in seen_urls:
                                seen_urls.add(url_r)
                                blocks.append(title + "\n  " + url_r)
                    if blocks:
                        results.extend(blocks[:6])
                        return True
                    source_errors["DuckDuckGo"] = "0 results"
                    return False
                except (_httpx.HTTPStatusError, _httpx.RequestError, _httpx.TimeoutException) as e:
                    source_errors["DuckDuckGo"] = type(e).__name__ + ": " + str(e)[:100]
                    return False

            # V67 P2-7：handler 级 seen_urls，跨源共享去重
            seen_urls: set[str] = set()

            # V67 P1-4：三源并发竞速 + 整体超时保护
            # 之前串行最坏 30s+（每源 10s），改为三源同时发起，取第一个成功的。
            # 总耗时 ≈ 单源超时 10s，而非累加。每个源返回 (bool, list[str]) 避免共享竞态。
            async def _try_source(src_name: str, fn) -> tuple[str, list[str]]:
                """运行单个源，返回 (源名, 结果列表)。失败返回 (源名, [])。"""
                try:
                    ok = await fn()
                    if ok:
                        return src_name, list(results)  # 快照当前 results
                    return src_name, []
                except Exception as e:
                    source_errors[src_name] = type(e).__name__ + ": " + str(e)[:100]
                    return src_name, []

            async def _run_search_race() -> str:
                nonlocal succeeded_source
                sources_tried.extend(["Bing", "360搜索", "DuckDuckGo"])
                # 三源并发
                tasks = [
                    asyncio.create_task(_try_source("Bing", _try_bing)),
                    asyncio.create_task(_try_source("360搜索", _try_360)),
                    asyncio.create_task(_try_source("DuckDuckGo", _try_ddg)),
                ]
                # 等所有完成，整体最多 12s（单源 10s + 余量）
                done, pending = await asyncio.wait(tasks, timeout=12.0)
                # 取消未完成的
                for t in pending:
                    t.cancel()
                # 从完成的任务中找第一个成功且有结果的
                # 优先级：Bing > 360 > DDG（按 tasks 顺序）
                final_results: list[str] = []
                for t in tasks:
                    if t in done and not t.cancelled():
                        try:
                            src_name, src_results = t.result()
                            if src_results:
                                succeeded_source = src_name
                                final_results = src_results
                                break
                        except Exception:
                            pass
                if final_results:
                    summary = "\n\n".join(final_results[:5])
                    src_label = succeeded_source or sources_tried[-1]
                    return (
                        "搜索结果（" + query + "，来源: " + src_label + "）：\n\n"
                        + summary
                        + "\n\n提示：基于上述结果直接回答用户。避免调用 web_search 多次。"
                    )
                # 全部源都失败时给 LLM 一个清晰诊断 + 替代方案
                err_parts = []
                for s in sources_tried:
                    err = source_errors.get(s, "unknown")
                    err_parts.append(s + "=" + err)
                err_summary = "; ".join(err_parts)
                sources_label = "、".join(sources_tried)
                return (
                    "[web_search: 全部源（" + sources_label + "）均失败。最后错误: " + err_summary + "。\n"
                    "替代方案（任选其一）：\n"
                    "1) 已知目标 URL → 用 system_run / python_execute 直接 curl 该 URL\n"
                    "   例: system_run(\"curl -sL 'https://example.com' | head -c 2000\")\n"
                    "2) 已知 API key → 用 python_execute 直接调 /v1/models 或 /v1/chat/completions\n"
                    "   注意：不要在命令字符串中明文包含 API key，用环境变量引用。\n"
                    "3) 无 URL/URL 都失败 → 直接基于已有知识回答，并在回复中说明「无实时网络信息」。\n"
                    "4) 用户明确说「在浏览器中打开」或「自己看」→ 不要再尝试，告知用户去访问官网。]"
                )

            try:
                return await asyncio.wait_for(_run_search_race(), timeout=25.0)
            except asyncio.TimeoutError:
                sources_label = "、".join(sources_tried) if sources_tried else "无"
                return (
                    "[web_search: 搜索超时（25s）。已尝试源: " + sources_label + "。"
                    "建议用 system_run + curl 直接获取目标 URL。]"
                )

        self.register(Skill(
            id="web_search", title="Web Search",
            description="搜索互联网获取最新信息。当你不确定答案、需要实时数据、"
                        "或用户询问近期事件时，使用此工具搜索网络。参数 input 为搜索关键词。",
            schema={
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "搜索互联网获取最新信息和实时数据，返回相关网页的标题、摘要和链接",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "input": {
                                "type": "string",
                                "description": "搜索关键词或问题",
                            },
                            "page": {
                                "type": "integer",
                                "description": "页码，默认1（第一页）。需要更多结果时用2、3等",
                            },
                        },
                        "required": ["input"],
                    },
                },
            },
            handler=web_search_handler,
        ))

        # ---------- 网页抓取技能 ----------
        async def web_fetch_handler(args: Dict[str, Any]) -> str:
            """抓取指定 URL 的网页内容，提取正文并转为可读文本。

            和 web_search 配合使用：先搜索找到相关 URL，再 fetch 获取全文。
            自动去除 HTML 标签、导航/广告噪声，提取正文内容。

            V67：SSRF 防护升级（重定向每跳校验 + IPv4-mapped IPv6 + ipaddress 模块）、
            charset 字节级检测、magic bytes 二进制检测、max_chars 上限。
            """
            url = str(args.get("url", "")).strip()
            if not url:
                return "[web_fetch error: empty url]"
            if not url.startswith(("http://", "https://")):
                return "[web_fetch error: url must start with http:// or https://]"

            # ---------- SSRF 防护 ----------
            # V67：用 ipaddress 模块覆盖所有私网/保留/回环/链路本地地址，
            # 支持 IPv4-mapped IPv6（::ffff:127.0.0.1）绕过检测。
            from urllib.parse import urlparse as _urlparse
            import ipaddress as _ipaddress
            import socket as _socket

            _BLOCKED_HOSTNAMES = {"localhost", "ip6-localhost", "ip6-loopback"}

            def _is_blocked_ip(ip_str: str) -> bool:
                """检查单个 IP 是否属于内网/回环/链路本地/保留地址。"""
                try:
                    addr = _ipaddress.ip_address(ip_str)
                except ValueError:
                    return False
                # IPv4-mapped IPv6（::ffff:1.2.3.4）会被 ip_address 解析为 IPv4Address
                return (
                    addr.is_private
                    or addr.is_loopback
                    or addr.is_link_local
                    or addr.is_reserved
                    or addr.is_multicast
                    or addr.is_unspecified
                )

            def _check_hostname_blocked(hostname: str) -> bool:
                """对 hostname 做 DNS 解析并检查所有返回 IP。"""
                if not hostname:
                    return True
                if hostname.lower() in _BLOCKED_HOSTNAMES:
                    return True
                # 如果 hostname 本身是 IP 字面量，直接检查
                try:
                    _ipaddress.ip_address(hostname)
                    # 是合法 IP，检查是否被阻止
                    return _is_blocked_ip(hostname)
                except ValueError:
                    pass  # 不是 IP 字面量，继续做 DNS 解析
                # DNS 解析
                try:
                    addrinfos = _socket.getaddrinfo(hostname, None)
                    for ai in addrinfos:
                        if _is_blocked_ip(ai[4][0]):
                            return True
                    return False
                except (_socket.gaierror, OSError):
                    return False  # DNS 解析失败，让 httpx 报错

            # 首次校验：原始 URL 的 hostname
            parsed = _urlparse(url)
            if _check_hostname_blocked(parsed.hostname or ""):
                return "[web_fetch error: 访问被拒绝 — 内网/回环/保留地址不允许]"

            # max_chars 上限保护（V67 P3-6）：防止 LLM 传超大值撑爆上下文
            try:
                max_chars = int(args.get("max_chars", 6000))
                if max_chars < 100:
                    max_chars = 6000
                elif max_chars > 20000:
                    max_chars = 20000
            except (ValueError, TypeError):
                max_chars = 6000

            import html as _htmlmod
            import re as _re2
            import httpx as _httpx2

            headers = {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }

            # V67 P0-1：重定向 SSRF 防护
            # follow_redirects=True 但 httpx 内置 transport 不校验重定向目标。
            # 用 event_hooks 在每个 request 发出前重新校验 hostname。
            async def _ssrf_request_hook(request: _httpx2.Request) -> None:
                redirect_host = _urlparse(str(request.url)).hostname or ""
                if _check_hostname_blocked(redirect_host):
                    raise _httpx2.RequestError(
                        "SSRF blocked: redirect to private/loopback address",
                        request=request,
                    )

            try:
                async with _httpx2.AsyncClient(
                    timeout=15.0,
                    follow_redirects=True,
                    max_redirects=3,
                    event_hooks={"request": [_ssrf_request_hook]},
                ) as client:
                    resp = await client.get(url, headers=headers)
            except (_httpx2.HTTPStatusError, _httpx2.RequestError, _httpx2.TimeoutException) as e:
                return "[web_fetch error: " + type(e).__name__ + ": " + str(e)[:200] + "]"

            if resp.status_code != 200:
                return "[web_fetch error: HTTP " + str(resp.status_code) + "]"

            content_type = resp.headers.get("content-type", "")

            # 二进制内容检测（V67 P2-6：补充 magic bytes 检测）
            # 1) 基于 Content-Type 头
            _binary_ct = (
                "image/", "video/", "audio/", "application/pdf",
                "application/zip", "application/octet-stream",
                "application/x-gzip", "application/x-tar",
                "application/x-7z", "application/x-rar",
            )
            is_binary_by_ct = any(ct in content_type for ct in _binary_ct)
            # 2) 基于内容魔数（即使服务器没返回 Content-Type 也能识别）
            raw_bytes = resp.content
            _MAGIC = {
                b"%PDF": "PDF",
                b"PK\x03\x04": "ZIP",
                b"\x1f\x8b": "GZIP",
                b"\x89PNG\r\n\x1a\n": "PNG",
                b"\xff\xd8\xff": "JPEG",
                b"GIF8": "GIF",
                b"BM": "BMP",
                b"Rar!\x1a\x07": "RAR",
                b"7z\xbc\xaf\x27\x1c": "7Z",
                b"\x25\x50\x44\x46": "PDF",  # %PDF 备用
            }
            binary_kind = ""
            if is_binary_by_ct:
                binary_kind = content_type
            else:
                for sig, kind in _MAGIC.items():
                    if raw_bytes[:len(sig)] == sig:
                        binary_kind = kind
                        break
            if binary_kind:
                return (
                    "[web_fetch: 目标是二进制内容（" + binary_kind + "），"
                    "无法提取文本。如需查看请用 system_run 下载。]\n\nURL: " + url
                )

            # 大页面处理（V67 P1-5：修正注释 + 字节级截断避免全量解码）
            # - Content-Length > 2MB 拒绝
            # - 字节流 > 512KB 截断到 512KB 再 decode，避免 2MB HTML 完整解码
            content_length = resp.headers.get("content-length", "")
            if content_length:
                try:
                    cl = int(content_length)
                    if cl > 2_097_152:  # 2MB
                        return (
                            "[web_fetch: 页面过大（" + str(cl // 1024) + "KB），"
                            "超过 2MB 限制。请用 system_run + curl 手动获取。]\n\nURL: " + url
                        )
                except ValueError:
                    pass

            # 字节级截断：超过 512KB 只取前 512KB
            _MAX_HTML_BYTES = 524_288  # 512KB
            if len(raw_bytes) > _MAX_HTML_BYTES:
                raw_bytes = raw_bytes[:_MAX_HTML_BYTES]

            # charset 处理（V67 P1-2：在 raw_bytes 上做字节级正则，避免乱码文本匹配失败）
            # httpx 默认用 Content-Type 头的 charset，但很多中文老站只在 <meta charset> 声明
            raw = resp.text  # httpx 自动解码的文本（可能用错编码）
            if "text/html" in content_type or "html" in content_type or raw_bytes[:500].lower().find(b"<html") >= 0:
                # 字节级正则检测 <meta charset=xxx>
                meta_match = _re2.search(
                    rb'<meta[^>]+charset=["\']?([\w-]+)',
                    raw_bytes[:2048],
                    _re2.IGNORECASE,
                )
                if meta_match:
                    try:
                        meta_charset = meta_match.group(1).decode("ascii", errors="ignore").strip().lower()
                    except Exception:
                        meta_charset = ""
                    current_encoding = (resp.encoding or "").lower()
                    if meta_charset and meta_charset != current_encoding and meta_charset in (
                        "gbk", "gb2312", "gb18030", "big5",
                        "utf-8", "utf8", "iso-8859-1", "latin-1",
                    ):
                        try:
                            raw = raw_bytes.decode(meta_charset, errors="replace")
                        except (LookupError, UnicodeDecodeError):
                            pass  # 未知编码，保持 httpx 默认

            # 提前截断超大 HTML（>100KB 字符），只处理前 100KB
            if len(raw) > 102400:
                raw = raw[:102400]

            # 如果是 JSON API 响应，直接返回
            if "json" in content_type or raw.strip().startswith(("{", "[")):
                try:
                    import json as _json
                    parsed = _json.loads(raw)
                    return "URL: " + url + "\n\n" + _json.dumps(parsed, ensure_ascii=False, indent=2)[:max_chars]
                except Exception:
                    pass

            # HTML → 可读文本
            text = raw

            # 1. 移除 script/style/nav/footer/aside/header 标签及内容
            for tag in ("script", "style", "nav", "footer", "aside", "header",
                        "noscript", "iframe", "svg", "form"):
                text = _re2.sub(
                    r"<" + tag + r"[^>]*>.*?</" + tag + r">",
                    " ",
                    text,
                    flags=_re2.DOTALL | _re2.IGNORECASE,
                )

            # 2. 提取 <article> 或 <main> 正文（如果有）
            article_m = _re2.search(
                r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>",
                text, _re2.DOTALL | _re2.IGNORECASE,
            )
            if article_m and len(article_m.group(1)) > 200:
                text = article_m.group(1)

            # 3. 段落和标题保留结构：把 </p>、<br>、</h*> 换成换行
            text = _re2.sub(r"</(?:p|div|h[1-6]|li|tr|blockquote)>", "\n", text, flags=_re2.IGNORECASE)
            text = _re2.sub(r"<br\s*/?>", "\n", text, flags=_re2.IGNORECASE)

            # 4. 去掉所有剩余 HTML 标签
            text = _re2.sub(r"<[^>]+>", " ", text)

            # 5. HTML 实体解码
            text = _htmlmod.unescape(text)

            # 6. 清理多余空行和空格
            lines = text.split("\n")
            cleaned_lines = []
            for line in lines:
                line = _re2.sub(r"[ \t]+", " ", line).strip()
                if line:
                    cleaned_lines.append(line)
            text = "\n".join(cleaned_lines)

            # 7. 如果正文太短，可能是 JS 渲染页面
            if len(text) < 50:
                return (
                    "[web_fetch: 页面内容过短（" + str(len(text)) + " 字符），"
                    "可能是 JS 渲染页面。尝试用 system_run + curl 获取原始 HTML，"
                    "或用 web_search 搜索其他来源。]\n\nURL: " + url
                )

            # 截断到 max_chars
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[... 已截断，原文共 " + str(len(text)) + " 字符]"

            return "URL: " + url + "\nHTTP " + str(resp.status_code) + "\n\n" + text

        self.register(Skill(
            id="web_fetch", title="Web Fetch",
            description="抓取指定 URL 的网页内容，提取正文并转为可读文本。"
                        "和 web_search 配合：先搜索找到 URL，再 fetch 获取全文。"
                        "参数 url 为目标网址，max_chars 为最大返回字符数（默认 6000）。",
            schema={
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": "抓取指定 URL 的网页正文内容，自动去除 HTML 标签和噪声",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "要抓取的目标网址（必须以 http:// 或 https:// 开头）",
                            },
                            "max_chars": {
                                "type": "integer",
                                "description": "最大返回字符数（默认 6000）",
                            },
                        },
                        "required": ["url"],
                    },
                },
            },
            handler=web_fetch_handler,
        ))

    def _seed_media_skills(self) -> None:
        # ---------- 语音转文字技能 ----------
        self.register(Skill(
            id="transcribe",
            title="语音转文字",
            description="将音频文件转换为文字（支持 Whisper）",
            handler=make_transcribe_handler(),
            schema={
                "type": "function",
                "function": {
                    "name": "transcribe",
                    "description": "将音频文件转录为文字，支持 Whisper。参数 path 为音频文件路径。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "音频文件路径（支持 mp3、wav、m4a 等格式）",
                            },
                            "input": {
                                "type": "string",
                                "description": "音频文件路径的别名（与 path 等效）",
                            },
                        },
                        "required": [],
                    },
                },
            },
        ))

        # ---------- 图片描述技能 ----------
        self.register(Skill(
            id="describe_image",
            title="图片描述",
            description="分析图片内容，回答关于图片的问题",
            handler=make_image_handler(),
            schema={
                "type": "function",
                "function": {
                    "name": "describe_image",
                    "description": "分析图片内容，回答关于图片的问题。参数 path 为图片路径，question 为可选问题。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "图片文件路径",
                            },
                            "input": {
                                "type": "string",
                                "description": "图片文件路径的别名（与 path 等效）",
                            },
                            "question": {
                                "type": "string",
                                "description": "关于图片的问题（可选，默认为'请描述这张图片'）",
                            },
                        },
                        "required": [],
                    },
                },
            },
        ))

    def _seed_doc_search_skill(self) -> None:
        # ---------- 文档搜索技能 (RAG) ----------
        self.register(Skill(
            id="document_search",
            title="文档搜索",
            description="搜索用户已上传的文档，支持 PDF、Markdown、TXT。"
                        "使用 action 参数控制操作：search（搜索）、list（列出文档）、ingest（摄入文档）。",
            schema={
                "type": "function",
                "function": {
                    "name": "document_search",
                    "description": "搜索已上传的文档内容，支持 PDF、Markdown、TXT 格式",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "操作类型：search（搜索文档内容）、list（列出已上传文档）、ingest（摄入新文档）",
                                "enum": ["search", "list", "ingest"],
                            },
                            "query": {
                                "type": "string",
                                "description": "搜索关键词（action=search 时使用）",
                            },
                            "input": {
                                "type": "string",
                                "description": "搜索关键词的别名（与 query 等效）",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "返回结果数量上限（默认 5）",
                            },
                            "path": {
                                "type": "string",
                                "description": "文档文件路径（action=ingest 时使用）",
                            },
                        },
                        "required": [],
                    },
                },
            },
            handler=make_doc_search_handler(get_doc_store()),
        ))

    def _seed_python_skill(self) -> None:
        # ---------- Python 代码执行技能 ----------
        # 使用全局共享的 PythonExecutor 实例（由 one_agent.py 创建并通过 ctx 传递）
        from executors.python_runner import make_python_handler
        python_executor = None

        # 尝试从 AgentContext 获取共享实例（在 start() 时设置）。
        # NOTE: SkillManager stores its context reference as self._ctx_ref
        # (set in setup()), NOT self._ctx — using the wrong name silently
        # fell through to creating a fresh executor every time, defeating
        # the shared-sandbox design.
        ctx_ref = getattr(self, "_ctx_ref", None)
        if ctx_ref is not None and hasattr(ctx_ref, "python_executor"):
            python_executor = ctx_ref.python_executor

        if python_executor is None:
            # 如果未提供，创建新实例（向后兼容或测试环境）
            from executors.python_runner import PythonExecutor
            python_executor = PythonExecutor()

        self.register(Skill(
            id="python_execute",
            title="Python 代码执行",
            description="在沙箱环境中执行 Python 代码，用于数学计算、数据处理、文件操作、API 调用、"
                        "网络请求、数据抓取、爬虫、脚本、自动化、编程、运行代码、写代码、"
                        "代码、Python、编程、运行、执行、脚本、API、请求、抓取、爬虫、数据采集、自动化。"
                        "支持安全的标准库（math, json, datetime, re 等），禁止系统调用和网络访问。",
            schema={
                "type": "function",
                "function": {
                    "name": "python_execute",
                    "description": "在沙箱环境中执行 Python 代码",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "要执行的 Python 代码",
                            },
                            "timeout": {
                                "type": "integer",
                                "description": "执行超时时间（秒，默认 10）",
                            },
                        },
                        "required": ["code"],
                    },
                },
            },
            handler=make_python_handler(python_executor),
        ))

    def _seed_model_management_skill(self) -> None:
        """模型管理技能：拉取 provider 模型、智能路由展示、交互式添加。"""

        async def model_manage_handler(args: Dict[str, Any]) -> str:
            """模型管理：添加模型 / 显示模型。

            用法：
              /添加模型 nvidia key:nvapi-xxx        — 已知 provider，拉取免费模型
              /添加模型 nvidia key:nvapi-xxx 免费    — 只拉取免费模型
              /添加模型 nvidia key:nvapi-xxx 全部    — 拉取所有模型
              /添加模型 myai key:xxx url:https://...  — 手动指定 API 地址
              /添加模型 someai key:xxx              — 未知 provider，自动探测+搜索
              /显示模型                              — 显示所有层级模型及能力
              /显示模型 免费                         — 只显示免费模型

            自然语言触发（无需斜杠命令，无需 key）：
              "商汤科技有新增加的免费模型，你拉取一下"
              → 自动识别 provider=商汤→sensenova，复用已配置 key，拉取免费模型
              "把 deepseek 的新模型添加进来"
              "刷新一下 nvidia 的模型列表"
            """
            input_text = str(args.get("input", "")).strip()
            ctx_ref = getattr(self, "_ctx_ref", None)
            if ctx_ref is None:
                return "❌ 无法访问系统上下文"

            llm = ctx_ref.get_plugin("llm")
            if llm is None:
                return "❌ LLM 提供器未初始化"

            # ---- /显示模型 ----
            if not input_text or input_text.startswith("显示") or input_text.startswith("list") or input_text.startswith("show"):
                return await _show_models(llm, input_text)

            # ---- /添加模型 ----
            import re as _re
            has_explicit_key = bool(_re.search(r'key[:\s]+\S+', input_text, _re.IGNORECASE))

            if has_explicit_key:
                # 显式带 key 的斜杠命令路径
                return await _add_models(llm, input_text)

            # ---- 自然语言路径：没有 key: 参数 ----
            # 尝试从自然语言里识别 provider，复用已配置的 key
            nl_result = _try_natural_language_fetch(llm, input_text)
            if nl_result is not None:
                provider, free_only, all_models_flag = nl_result
                return await _fetch_models_with_existing_key(
                    llm, provider, free_only, all_models_flag,
                )

            # 既没 key 也不是可识别的自然语言请求 → 显示用法
            return _add_models_usage()

        self.register(Skill(
            id="model_manage", title="Model Manager",
            description=(
                "/添加模型 或 /显示模型：管理智能路由模型。"
                "支持自然语言触发：拉取模型、添加模型、刷新模型、获取模型、"
                "导入模型、更新模型、商汤、deepseek、nvidia、ollama 等provider模型管理。"
            ),
            schema=_schema("model_manage", "manage models and routing", []),
            handler=model_manage_handler,
        ))

    def _auto_schema(self, handler: Callable, description: str = "") -> Dict[str, Any]:
        """V69: 使用 auto_tool_schema 从 handler 函数签名/docstring 自动生成 JSON Schema。

        与手写 _schema() 互补：handler 有完整类型注解和 docstring 时，
        auto_tool_schema 能生成比手写更精确的参数描述。

        Args:
            handler: 技能处理函数（同步或异步）
            description: 可选描述，覆盖 docstring 提取的描述

        Returns:
            OpenAI Function Calling 格式的 schema 字典
        """
        from skills.schema_gen import auto_tool_schema
        schema = auto_tool_schema(handler)
        if description:
            schema.setdefault("function", {})["description"] = description
        return schema

    def _seed_builtins(self) -> None:
        """Seed built-in skills that are always available."""
        self._seed_core_skills()
        self._seed_system_skills()
        self._seed_lifecycle_skills()
        self._seed_settings_skill()
        self._seed_model_management_skill()
        self._seed_web_search_skill()
        self._seed_media_skills()
        self._seed_doc_search_skill()
        self._seed_python_skill()
        self._seed_tool_ecosystem_skills()  # Round 7: email/calendar/db/openapi/chart

    def _seed_tool_ecosystem_skills(self) -> None:
        """Register Round 7 tool ecosystem skills so the LLM can call them as tools."""
        # Email — handler 接收 dict (Skill.run 传 args dict)
        async def _email_handler(args: dict) -> str:
            """Read and send emails via IMAP/SMTP.

            Args:
                args: Email command dict, e.g. {"action": "read"},
                    {"action": "send", "to": "...", "subject": "...", "body": "..."},
                    or {"action": "search", "query": "..."}
            """
            from skills.email import get_email_skill
            skill = get_email_skill()
            # args 可能是 dict 或 str
            if isinstance(args, str):
                import shlex
                parts = shlex.split(args.strip())
                action = parts[0].lower() if parts else "read"
                args = {"action": action}
                if action == "send" and len(parts) >= 4:
                    args.update({"to": parts[1], "subject": parts[2], "body": parts[3]})
                elif action == "search" and len(parts) >= 2:
                    args.update({"query": parts[1]})
            elif isinstance(args, dict):
                pass  # already a dict
            else:
                args = {"action": "read"}
            return await skill.run(args)

        self.register(Skill(
            id="email",
            title="Email",
            description="/email <read|send|search> — 读写邮件 (IMAP/SMTP)",
            schema=_try_auto_schema(
                _email_handler,
                _schema("email", "Read and send emails", []),
                description="Read and send emails",
            ),
            handler=_email_handler,
        ))

        # Calendar
        async def _calendar_handler(args: dict) -> str:
            """Manage calendar events (Google Calendar / CalDAV).

            Args:
                args: Calendar command dict, e.g. {"action": "list"},
                    {"action": "today"}, {"action": "week"},
                    or {"action": "create", "title": "..."}
            """
            from skills.calendar import get_calendar_skill
            skill = get_calendar_skill()
            if isinstance(args, str):
                parts = args.strip().split(None, 1)
                action = parts[0].lower() if parts else "list"
                args = {"action": action}
                if action == "create" and len(parts) > 1:
                    args["title"] = parts[1]
            elif not isinstance(args, dict):
                args = {"action": "list"}
            return await skill.run(args)

        self.register(Skill(
            id="calendar",
            title="Calendar",
            description="/calendar <list|today|week|create> — 管理日程",
            schema=_try_auto_schema(
                _calendar_handler,
                _schema("calendar", "Manage calendar events", []),
                description="Manage calendar events",
            ),
            handler=_calendar_handler,
        ))

        # Database
        async def _db_handler(args: dict) -> str:
            """Query databases (PostgreSQL/MySQL/SQLite).

            Args:
                args: Database command dict, e.g. {"action": "tables"}
                    or {"action": "query", "sql": "SELECT * FROM users"}
            """
            from skills.database import get_database_skill
            skill = get_database_skill()
            if isinstance(args, str):
                parts = args.strip().split(None, 1)
                action = parts[0].lower() if parts else "tables"
                args = {"action": action}
                if action == "query" and len(parts) > 1:
                    args["sql"] = parts[1]
            elif not isinstance(args, dict):
                args = {"action": "tables"}
            return await skill.run(args)

        self.register(Skill(
            id="database",
            title="Database",
            description="/db <tables|query <sql>> — 查询数据库",
            schema=_try_auto_schema(
                _db_handler,
                _schema("database", "Query databases (PostgreSQL/MySQL/SQLite)", []),
                description="Query databases (PostgreSQL/MySQL/SQLite)",
            ),
            handler=_db_handler,
        ))

        # OpenAPI
        async def _openapi_handler(args, llm=None, **kw):
            from skills.openapi import get_openapi_skill
            skill = get_openapi_skill()
            if isinstance(args, str):
                parts = args.strip().split(None, 1)
                action = parts[0].lower() if parts else "list"
                args = {"action": action}
                if action == "load" and len(parts) > 1:
                    args["url"] = parts[1]
            elif not isinstance(args, dict):
                args = {"action": "list"}
            return await skill.run(args)

        self.register(Skill(
            id="openapi",
            title="OpenAPI",
            description="/openapi <load <url>|list|search> — 解析OpenAPI文档",
            schema=_schema("openapi", "Parse OpenAPI specs and call APIs", []),
            handler=_openapi_handler,
        ))

        # Chart
        async def _chart_handler(args, llm=None, **kw):
            from core.chart_gen import get_chart_generator
            skill = get_chart_generator()
            if isinstance(args, str):
                import json
                parts = args.strip().split(None, 1)
                chart_type = parts[0] if parts else "flowchart"
                data = {}
                if len(parts) > 1:
                    try:
                        data = json.loads(parts[1])
                    except json.JSONDecodeError:
                        data = {"title": parts[1]}
                args = {"chart_type": chart_type, "data": data}
            elif not isinstance(args, dict):
                args = {"chart_type": "flowchart", "data": {}}
            return await skill.run(args)

        self.register(Skill(
            id="chart",
            title="Chart Generator",
            description="/chart <type> <json> — 生成图表 (flowchart/pie/gantt/sequence等)",
            schema=_schema("chart", "Generate charts and diagrams", []),
            handler=_chart_handler,
        ))

        # Round 8: MCP client — 连接外部 MCP 服务器的工具
        # 之前 mcp_client.py 完全没注册，现在通过 mcp_call 工具暴露
        async def _mcp_call_handler(args, llm=None, **kw):
            from skills.mcp_client import MCPClient
            client = MCPClient()
            if isinstance(args, str):
                args = {"input": args}
            input_str = args.get("input", "") or args.get("server", "")
            # 简单解析：server_name | tool_name | arguments
            parts = [p.strip() for p in input_str.split("|", 2)]
            if len(parts) < 2:
                return "[MCP call 需要格式: server_name | tool_name | args_json]"
            server_name, tool_name = parts[0], parts[1]
            tool_args = {}
            if len(parts) >= 3 and parts[2]:
                import json
                try:
                    tool_args = json.loads(parts[2])
                except json.JSONDecodeError:
                    tool_args = {"input": parts[2]}
            try:
                # 从 config 读取 MCP 服务器列表
                mcp_servers = (self._mcp_servers or [])
                server_cfg = next((s for s in mcp_servers if s.get("name") == server_name), None)
                if not server_cfg:
                    return f"[MCP 服务器 {server_name} 未配置。请在 config 中设置 mcp_servers]"
                if server_name not in client.servers:
                    await client.add_server(
                        name=server_name,
                        url=server_cfg.get("url", ""),
                        api_key=server_cfg.get("api_key"),
                    )
                result = await client.call_tool(server_name, tool_name, tool_args)
                try:
                    self.bus.publish({"type": "mcp_tool_called", "tool": tool_name, "server": server_name})
                except Exception:
                    pass
                return str(result)[:2000] if result else "[MCP 调用无返回]"
            except Exception as exc:
                return f"[MCP call 失败: {exc}]"

        self.register(Skill(
            id="mcp_call",
            title="MCP Tool Caller",
            description="/mcp_call <server> | <tool> | <args> — 调用已配置的 MCP 服务器工具。需要在 config mcp.servers 中预先配置服务器。",
            schema=_schema("mcp_call", "Call a tool on a configured MCP server. Format: 'server_name | tool_name | args_json'", ["input"]),
            handler=_mcp_call_handler,
        ))

        # Round 8: MCP server — 启动/停止本地 MCP 服务器
        async def _mcp_server_handler(args, llm=None, **kw):
            from skills.mcp_server import get_mcp_server
            server = get_mcp_server()
            if isinstance(args, str):
                args = {"input": args}
            action = args.get("action", "status")
            input_str = args.get("input", action)
            if "start" in input_str or "启动" in input_str:
                port = int(args.get("port", 8765))
                await server.start(port=port)
                return f"[MCP 服务器已启动，端口 {port}]"
            elif "stop" in input_str or "停止" in input_str:
                await server.stop()
                return "[MCP 服务器已停止]"
            else:
                return f"[MCP 服务器状态: {'运行中' if server._running else '未启动'}]"

        self.register(Skill(
            id="mcp_server",
            title="MCP Server Manager",
            description="/mcp_server start|stop|status — 启动/停止/查看本地 MCP 服务器。",
            schema=_schema("mcp_server", "Manage the local MCP server lifecycle", []),
            handler=_mcp_server_handler,
        ))

        # Round 8: 深度研究 — 自动注册为 skill，LLM 可通过 tool-calling 调用
        async def _deep_research_handler(args, llm=None, **kw):
            from core.deep_research import get_deep_researcher
            if isinstance(args, str):
                args = {"input": args}
            question = args.get("input", "") or args.get("question", "")
            if not question or len(question) < 5:
                return "[深度研究需要提供研究问题]"
            researcher = get_deep_researcher(llm, self)
            def on_progress(phase, msg):
                pass  # 静默执行，coordinator 层会处理进度
            report = await researcher.research(
                question=question, model=None, depth=2, on_progress=on_progress,
            )
            return researcher.format_report(report)

        self.register(Skill(
            id="deep_research",
            title="Deep Researcher",
            description="对复杂问题进行多轮深度研究：分解子问题→搜索→分析→综合报告。适用于需要广泛信息收集和分析的问题。",
            schema=_schema("deep_research", "Conduct deep research on a complex question. Provide the research question.", ["input"]),
            handler=_deep_research_handler,
        ))

        # Round 8: 批量处理 — 自动注册为 skill
        async def _batch_process_handler(args, llm=None, **kw):
            from core.batch import get_batch_processor
            if isinstance(args, str):
                args = {"input": args}
            text = args.get("input", "")
            if not text or len(text) < 10:
                return "[批量处理需要提供内容和任务类型]"
            processor = get_batch_processor(llm, self)
            lines = text.split("\n", 1)
            task_type = "general"
            content = text
            if len(lines) >= 2:
                first_word = lines[0].strip().lower()
                if first_word in ("translate", "翻译", "summarize", "总结", "classify", "分类", "extract", "提取"):
                    task_type_map = {
                        "translate": "translate", "翻译": "translate",
                        "summarize": "summarize", "总结": "summarize",
                        "classify": "classify", "分类": "classify",
                        "extract": "extract", "提取": "extract",
                    }
                    task_type = task_type_map.get(first_word, "general")
                    content = lines[1].strip()
            items = processor.split_items(content)
            if len(items) < 2:
                return "[批量处理需要至少 2 个项目]"
            results = await processor.process(items, task_type=task_type, model=None)
            return processor.format_results(results)

        self.register(Skill(
            id="batch_process",
            title="Batch Processor",
            description="批量处理多个项目（翻译、总结、分类、提取）。输入格式：第一行任务类型，后续每行一个项目。",
            schema=_schema("batch_process", "Batch process multiple items. First line: task type (translate/summarize/classify/extract). Following lines: items.", ["input"]),
            handler=_batch_process_handler,
        ))

        # Round 8: 工作流引擎 — 自动注册为 skill
        async def _workflow_run_handler(args, llm=None, **kw):
            from core.workflow_engine import get_workflow_engine
            import json as _json
            if isinstance(args, str):
                args = {"input": args}
            wf_text = args.get("input", "")
            try:
                workflow = _json.loads(wf_text.strip())
            except _json.JSONDecodeError:
                return "[工作流需要 JSON 格式定义]"
            engine = get_workflow_engine(llm, self)
            result = await engine.execute(workflow)
            return f"工作流完成: {result.status.value}\n步骤: {len(result.steps)}"

        self.register(Skill(
            id="workflow_run",
            title="Workflow Runner",
            description="执行多步骤工作流。输入 JSON 格式的工作流定义。",
            schema=_schema("workflow_run", "Run a multi-step workflow defined in JSON.", ["input"]),
            handler=_workflow_run_handler,
        ))


def _schema(name: str, description: str, required: List[str]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"input": {"type": "string", "description": "input data"}},
                "required": required,
            },
        },
    }


def _try_auto_schema(handler: Callable, fallback_schema: Dict[str, Any], description: str = "") -> Dict[str, Any]:
    """V69: 优先用 auto_tool_schema 自动生成 schema，失败/不完整则 fallback 到手写。

    Args:
        handler: 技能处理函数，需有类型注解和 docstring 才能生成完整 schema
        fallback_schema: 手写 _schema() 生成的兜底 schema
        description: 可选描述，覆盖 docstring 提取的描述

    Returns:
        自动生成的 schema（成功时）或 fallback_schema（失败时）
    """
    try:
        from skills.schema_gen import auto_tool_schema
        auto = auto_tool_schema(handler)
        props = (auto or {}).get("function", {}).get("parameters", {}).get("properties")
        if auto and props:
            # auto_tool_schema 用 func.__name__ 作工具名，这里对齐 skill id 保持一致
            auto["function"]["name"] = fallback_schema["function"]["name"]
            if description:
                auto["function"]["description"] = description
            return auto
    except Exception as exc:
        logger.debug("auto_tool_schema failed for %s: %s", handler, exc)
    return fallback_schema


# ---------- 设置管理：自然语言配置读写 ----------

# 配置键的中文/英文别名映射
_SETTING_ALIASES: Dict[str, tuple] = {
    # (yaml_path, value_type)
    "模型": ("llm.primary_model", str),
    "model": ("llm.primary_model", str),
    "主模型": ("llm.primary_model", str),
    "轻量模型": ("llm.lightweight_model", str),
    "本地模型": ("llm.local_model", str),
    "温度": ("llm.default_temperature", float),
    "temperature": ("llm.default_temperature", float),
    "最大token": ("llm.default_max_tokens", int),
    "max_tokens": ("llm.default_max_tokens", int),
    "超时": ("llm.timeout", int),
    "timeout": ("llm.timeout", int),
    "重试": ("llm.retries", int),
    "retries": ("llm.retries", int),
    "提供商": ("llm.primary_provider", str),
    "provider": ("llm.primary_provider", str),
    "日志级别": ("agent.log_level", str),
    "log_level": ("agent.log_level", str),
    "时区": ("agent.timezone", str),
    "timezone": ("agent.timezone", str),
    "数据目录": ("agent.data_dir", str),
    # 网关开关
    "web": ("gateways.web.enabled", bool),
    "telegram": ("gateways.telegram.enabled", bool),
    "企业微信": ("gateways.wecom.enabled", bool),
    "wecom": ("gateways.wecom.enabled", bool),
    "微信": ("gateways.wechat_personal.enabled", bool),
    "个人微信": ("gateways.wechat_personal.enabled", bool),
    "wechat": ("gateways.wechat_personal.enabled", bool),
    "钉钉": ("gateways.dingtalk.enabled", bool),
    "dingtalk": ("gateways.dingtalk.enabled", bool),
    "飞书": ("gateways.feishu.enabled", bool),
    "feishu": ("gateways.feishu.enabled", bool),
    "discord": ("gateways.discord.enabled", bool),
    "slack": ("gateways.slack.enabled", bool),
    # 执行环境
    "docker": ("execution.docker.enabled", bool),
    "shell": ("execution.local_shell.enabled", bool),
    "浏览器": ("execution.browser.enabled", bool),
    "browser": ("execution.browser.enabled", bool),
    "docker镜像": ("execution.docker.image", str),
    "docker内存": ("execution.docker.memory_limit_mb", int),
    # 记忆
    "长期记忆": ("memory.long_term.enabled", bool),
    "程序记忆": ("memory.procedural.enabled", bool),
    "记忆衰减": ("memory.long_term.decay_enabled", bool),
    # 路由
    "路由": ("router.enabled", bool),
    "上下文压缩": ("router.context_compression.enabled", bool),
    "自进化": ("router.self_evolution.enabled", bool),
    # 缓存
    "缓存": ("llm_cache.enabled", bool),
    "缓存大小": ("llm_cache.max_size", int),
    "缓存时间": ("llm_cache.ttl_seconds", int),
    # 多模态
    "多模态": ("multimodal.enabled", bool),
    "multimodal": ("multimodal.enabled", bool),
    # 调度器
    "调度器": ("scheduler.enabled", bool),
    "scheduler": ("scheduler.enabled", bool),
    # 安全
    "加密": ("security.data_encryption.enabled", bool),
    "审计日志": ("security.audit_log.enabled", bool),
    # API
    "api端口": ("rest.port", int),
    "api_key": ("rest.api_key", str),
    # 安全
    "允许聊天改密钥": ("security.allow_sensitive_chat_settings", bool),
    "allow_sensitive": ("security.allow_sensitive_chat_settings", bool),
}

# 敏感配置（默认不允许通过对话修改，受 security.allow_sensitive_chat_settings 控制）
# security.allow_sensitive_chat_settings itself is sensitive — otherwise an
# attacker could enable it via chat and then modify all other sensitive keys.
_SENSITIVE_KEYS = {"rest.api_key", "llm.api_keys", "security.allow_sensitive_chat_settings"}


def _is_sensitive_write_allowed(config: dict) -> bool:
    """检查是否允许通过对话修改敏感配置。"""
    return bool(_get_nested(config, "security.allow_sensitive_chat_settings", False))


def _get_nested(d: dict, path: str, default=None):
    """按点分隔路径获取嵌套字典值。"""
    keys = path.split(".")
    current = d
    for k in keys:
        if isinstance(current, dict):
            current = current.get(k, default)
        else:
            return default
    return current


def _set_nested(d: dict, path: str, value) -> None:
    """按点分隔路径设置嵌套字典值。"""
    keys = path.split(".")
    if not keys:
        raise ValueError("Path cannot be empty")
    current = d
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    if keys[-1]:
        current[keys[-1]] = value


def _parse_bool_value(text: str) -> Optional[bool]:
    """从自然语言中解析布尔值。"""
    t = text.lower().strip()
    if t in {"true", "yes", "1", "on", "开", "开启", "启用", "打开", "是", "enable", "enabled"}:
        return True
    if t in {"false", "no", "0", "off", "关", "关闭", "禁用", "停用", "否", "disable", "disabled"}:
        return False
    return None


def _parse_value(text: str, value_type: type):
    """根据目标类型解析用户输入的值。"""
    text = text.strip().strip("\"'").strip()
    if value_type is bool:
        v = _parse_bool_value(text)
        if v is not None:
            return v
        return None
    if value_type is int:
        import re as _re
        m = _re.search(r"-?\d+", text)
        return int(m.group()) if m else None
    if value_type is float:
        import re as _re
        m = _re.search(r"-?\d+\.?\d*", text)
        return float(m.group()) if m else None
    return text  # str


def _process_settings_command(input_text: str, config: dict, bus=None) -> str:
    """解析自然语言设置命令，读取或修改配置。"""

    lower = input_text.lower()

    # ---- 列出所有设置 ----
    if re.search(r"列出|所有|全部|list|all|show.?all", lower):
        lines = ["当前配置：\n"]
        sensitive_allowed = _is_sensitive_write_allowed(config)
        for alias, (path, _) in _SETTING_ALIASES.items():
            if any(c.isascii() and c.isalpha() for c in alias) and any("\u4e00" <= c <= "\u9fff" for c in alias):
                continue  # 跳过中英混合别名，避免重复
            val = _get_nested(config, path, "(未设置)")
            # 敏感值脱敏
            if any(sk in path for sk in _SENSITIVE_KEYS) and isinstance(val, str) and len(val) > 8:
                if not sensitive_allowed:
                    val = val[:4] + "****"
            lines.append(f"  {alias} = {val}")
        return "\n".join(lines)

    # ---- 读取设置 ----
    # 匹配 "查看模型" / "当前温度" / "show model" / "什么模型" / "模型是什么"
    read_patterns = [
        r"查看|查看一下|当前|显示|show|get|什么|是啥|是多少|是什么",
    ]
    is_read = any(re.search(p, lower) for p in read_patterns)

    # ---- 修改设置 ----
    # 匹配 "改成/改为/设置/设为/改成/切换/turn on/off/enable/disable/开启/关闭"
    write_patterns = [
        r"改成|改为|设置|设为|切换|修改|换成|调整|set|change|turn.?on|turn.?off|enable|disable|开启|关闭|启用|禁用|打开",
    ]
    is_write = any(re.search(p, lower) for p in write_patterns)

    if not is_read and not is_write:
        # 尝试推断：如果包含值，则是写操作；否则是读操作
        is_write = bool(re.search(r"\d+|true|false|开|关|启用|禁用|on|off|yes|no", lower))

    # 查找匹配的配置键
    matched_alias = None
    matched_path = None
    matched_type = str
    for alias, (path, vtype) in _SETTING_ALIASES.items():
        if alias in lower or alias.lower() in lower:
            matched_alias = alias
            matched_path = path
            matched_type = vtype
            break

    if matched_path is None:
        # 模糊匹配：检查用户输入是否包含别名关键词
        for alias, (path, vtype) in _SETTING_ALIASES.items():
            # 对中文别名做子串匹配
            if len(alias) >= 2 and alias in input_text:
                matched_alias = alias
                matched_path = path
                matched_type = vtype
                break
            # 对英文别名做单词匹配
            if alias.isascii() and re.search(r"\b" + re.escape(alias) + r"\b", lower):
                matched_alias = alias
                matched_path = path
                matched_type = vtype
                break

    if matched_path is None:
        chinese_aliases = [a for a in _SETTING_ALIASES if any('\u4e00' <= c <= '\u9fff' for c in a)]
        return f"未识别的设置项。可设置的选项：{', '.join(chinese_aliases)}"

    # 敏感项写入检查
    if is_write and any(sk in matched_path for sk in _SENSITIVE_KEYS):
        if not _is_sensitive_write_allowed(config):
            return (
                f"⚠️ {matched_alias} 是安全敏感项，默认不允许在聊天中修改。\n"
                f"如需开启此功能，请设置 security.allow_sensitive_chat_settings: true\n"
                f"或直接编辑配置文件 config/default_config.yaml"
            )
        # 允许修改但显示警告
        logger.warning("用户通过聊天修改敏感配置: %s", matched_path)

    # ---- 执行读取 ----
    if not is_write:
        current_val = _get_nested(config, matched_path, "(未设置)")
        # 敏感值脱敏：未开启 allow_sensitive_chat_settings 时隐藏
        if any(sk in matched_path for sk in _SENSITIVE_KEYS):
            if isinstance(current_val, str) and len(current_val) > 8:
                if not _is_sensitive_write_allowed(config):
                    current_val = current_val[:4] + "****"
        return f"{matched_alias} = {current_val}"

    # ---- 执行修改 ----
    # 从用户输入中提取新值
    # 策略：找到别名后面的内容作为值
    value_text = ""
    if matched_alias:
        idx = lower.find(matched_alias.lower()) if matched_alias.isascii() else input_text.find(matched_alias)
        if idx >= 0:
            value_text = input_text[idx + len(matched_alias):].strip()
            # 去掉连接词
            for prefix in ["改成", "改为", "设置", "设为", "切换", "修改", "换成", "调整", "为", "成", "to", "=", "：", ":"]:
                if value_text.lower().startswith(prefix):
                    value_text = value_text[len(prefix):].strip()

    if not value_text:
        # 尝试从整句中提取值
        for pat in [r"为\s*(.+)", r"成\s*(.+)", r"to\s+(.+)", r"=\s*(.+)"]:
            m = re.search(pat, lower)
            if m:
                value_text = m.group(1).strip()
                break

    if not value_text:
        return f"请指定 {matched_alias} 的新值。例如：'把{matched_alias}改为xxx'"

    new_value = _parse_value(value_text, matched_type)
    if new_value is None:
        return f"无法解析值 '{value_text}'，期望类型：{matched_type.__name__}"

    # 应用修改
    _set_nested(config, matched_path, new_value)

    # 持久化到配置文件
    _save_config(config, bus=bus)

    return f"已将 {matched_alias} 修改为 {new_value}"


def _save_config(config: dict, bus=None) -> None:
    """将配置写回 YAML 文件（原子写入，带文件锁）。

    安全修复：
    1. 写盘前调用 `_sanitize_config_for_persist` 脱敏——内存中的 config
       来自 ctx.config，已把 ${VAR} 展开为明文、enc:xxx 解密为明文 API key。
       直接 dump 会把明文密钥永久固化到配置文件。脱敏把敏感字段的值
       还原为 ${ENV_VAR} 占位符（若能匹配环境变量）或 null。
    2. 临时文件清理改为 try/finally + temp_path 预初始化为 None，避免
       yaml.YAMLError 抛出时 temp_path 未定义导致临时文件残留磁盘
       （原 except OSError 不捕获 YAMLError）。
    """
    import tempfile

    import yaml
    config_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
    lock_path = config_path + ".lock"

    # 写盘前脱敏，避免明文密钥落盘
    sanitized = _sanitize_config_for_persist(config)

    # Cross-platform file locking
    lock_fd = None
    use_fcntl = False
    try:
        import fcntl
        use_fcntl = True
    except ImportError:
        # Windows: use msvcrt or skip locking
        try:
            import msvcrt
        except ImportError:
            logger.warning("File locking not available on this platform")

    temp_path: str | None = None  # 预初始化，确保 finally 中可访问
    try:
        # Acquire file lock to prevent race conditions
        lock_fd = open(lock_path, "w")
        try:
            if use_fcntl:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            else:
                # Windows: try msvcrt locking
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
                except (ImportError, OSError):
                    pass  # Skip locking if not available

            # Write to temp file first, then atomically rename
            dir_name = os.path.dirname(config_path) or "."
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".yaml",
                dir=dir_name,
                delete=False,
            ) as f:
                yaml.dump(sanitized, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                temp_path = f.name
            # Atomic rename
            os.replace(temp_path, config_path)
            temp_path = None  # 已成功 rename，无需清理
            # Publish config_changed event
            if bus is not None:
                try:
                    bus.publish({"type": "config_changed"})
                except Exception:
                    pass
        finally:
            if use_fcntl and lock_fd:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception as exc:
                    logger.debug("ignored non-critical error: %s", exc)
            elif lock_fd:
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                except (ImportError, OSError):
                    pass
            if lock_fd:
                lock_fd.close()
            try:
                os.unlink(lock_path)
            except OSError as exc:
                logger.error("failed to unlink lock file %s: %s", lock_path, exc, exc_info=True)
    except Exception as exc:
        # 改为 except Exception 而非 OSError，捕获 yaml.YAMLError 等所有异常
        logger.error("保存配置失败: %s", exc, exc_info=True)
    finally:
        # 兜底清理临时文件（无论成功失败）
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError as exc2:
                logger.error("failed to clean up temp file %s: %s", temp_path, exc2, exc_info=True)


# 敏感键名集合（与 models/recommend.py 保持一致）
_CONFIG_SENSITIVE_KEYS = {
    "api_key", "api_keys", "apikey", "secret", "secret_key",
    "token", "access_token", "refresh_token",
    "private_key", "client_secret",
}


def _sanitize_config_for_persist(cfg, _seen=None):
    """递归脱敏 config dict，把展开的密钥还原为 ${ENV_VAR} 或 null。

    与 models/recommend.py 的 _sanitize_for_persist 等价——独立实现以
    避免循环依赖（skills 在某些场景下先于 models 加载）。
    """
    import os
    if _seen is None:
        _seen = set()
    if id(cfg) in _seen:
        return cfg  # 防止循环引用
    _seen.add(id(cfg))
    if isinstance(cfg, dict):
        out = {}
        for k, v in cfg.items():
            key_lower = str(k).lower()
            if key_lower in _CONFIG_SENSITIVE_KEYS:
                out[k] = _redact_config_value(v, _seen)
            else:
                out[k] = _sanitize_config_for_persist(v, _seen)
        return out
    if isinstance(cfg, list):
        return [_sanitize_config_for_persist(item, _seen) for item in cfg]
    return cfg


def _redact_config_value(value, _seen):
    """把单个敏感值还原为 ${ENV_VAR} 或 null。"""
    import os
    if isinstance(value, dict):
        return {k: _redact_config_value(v, _seen) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_config_value(item, _seen) for item in value]
    if not isinstance(value, str):
        return value
    # enc: 加密内容保留（本身可安全落盘）
    if value.startswith("enc:"):
        return value
    # ${VAR} 占位符保留
    if value.startswith("${") and value.endswith("}"):
        return value
    # 空值保留
    if not value:
        return value
    # 尝试在 env 中找到与 value 相等的变量名
    for env_name, env_val in os.environ.items():
        if env_val == value and _is_safe_env_name(env_name):
            return f"${{{env_name}}}"
    # 找不到对应 env var：写 null，避免明文落盘
    return None


def _is_safe_env_name(name: str) -> bool:
    """只把可识别的密钥类 env var 还原为占位符。

    防止把普通 env var（如 PATH=/usr/bin）误还原为 ${PATH}。
    """
    upper = name.upper()
    return any(
        kw in upper for kw in (
            "API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD",
            "PRIVATE_KEY", "CLIENT_SECRET", "ACCESS_TOKEN",
        )
    )


# ============================================================
# 模型管理辅助函数
# ============================================================

# 层级中文描述
_TIER_INFO = {
    "trivial":  {"name": "第1层·轻量",   "desc": "极简单任务（打招呼、查时间）", "icon": "🟢"},
    "simple":   {"name": "第2层·标准",   "desc": "简单对话和常见问题",           "icon": "🔵"},
    "complex":  {"name": "第3层·高级",   "desc": "复杂推理、代码、多步骤任务",   "icon": "🟡"},
    "expert":   {"name": "第4层·专家",   "desc": "极复杂推理、长文分析、专家级", "icon": "🔴"},
}
_TIER_ORDER = ["trivial", "simple", "complex", "expert"]


def _model_capability_desc(model_id: str) -> str:
    """从模型名推断能力描述。"""
    name = model_id.lower()
    caps = []
    if any(k in name for k in ("vision", "image", "multimodal", "vl")):
        caps.append("视觉")
    if any(k in name for k in ("code", "coder", "coding")):
        caps.append("代码")
    if any(k in name for k in ("reason", "thinking", "r1", "o1", "o3")):
        caps.append("推理")
    if any(k in name for k in ("70b", "72b", "405b", "ultra", "max", "opus", "sonnet")):
        caps.append("大参数")
    if any(k in name for k in ("8b", "7b", "3b", "mini", "haiku", "flash", "nano", "lite")):
        caps.append("轻量")
    if any(k in name for k in ("tool", "function")):
        caps.append("工具")
    return "、".join(caps) if caps else "通用"


def _model_cost_desc(model_id: str) -> str:
    """从 MODEL_COST 查询价格。

    区分「未登记价格」（可能是付费，只是我们不知道）和「明确免费」。
    原代码用 MODEL_COST.get(model_id, 0)，把所有未登记模型当成 0 → 免费，
    导致 /显示模型 免费 把一堆付费模型误判为免费。
    """
    from models.tiers import MODEL_COST
    if model_id not in MODEL_COST:
        return "未知"
    cost = MODEL_COST[model_id]
    if cost == 0:
        return "免费"
    if cost < 0.001:
        return f"${cost}/1K"
    return f"${cost:.4f}/1K"


async def _show_models(llm, input_text: str) -> str:
    """显示所有层级模型及能力。"""
    from models.tiers import MODEL_TIERS

    free_only = any(k in input_text for k in ("免费", "free", "free_only"))

    lines = ["📡 智能路由模型列表", "=" * 50, ""]

    for tier in _TIER_ORDER:
        info = _TIER_INFO[tier]
        models = MODEL_TIERS.get(tier, [])
        if free_only:
            # 过滤：只显示免费模型（cost=0 或名字含 free）
            models = [m for m in models if _model_cost_desc(m) == "免费"]

        lines.append(f"{info['icon']} {info['name']} — {info['desc']}")
        lines.append(f"   复杂度阈值见 config/default_config.yaml → router.task_complexity_thresholds")

        if not models:
            lines.append("   （暂无模型）")
        else:
            for idx, model_id in enumerate(models):
                role = "主模型" if idx == 0 else f"备选{idx}"
                caps = _model_capability_desc(model_id)
                cost = _model_cost_desc(model_id)
                # 检查是否有可用 key
                provider = model_id.split("/")[0] if "/" in model_id else ""
                has_key = llm._has_usable_key(provider) if provider else False
                key_status = "✅" if has_key else "⚠️ 无key"
                lines.append(f"   {idx+1}. [{role}] {model_id}")
                lines.append(f"      能力: {caps} | 费用: {cost} | {key_status}")
        lines.append("")

    has_any = any(MODEL_TIERS.get(t) for t in _TIER_ORDER)
    if not has_any:
        lines.append("⚠️ 当前没有任何模型配置。使用 /添加模型 来添加。")

    lines.append("=" * 50)
    lines.append("💡 提示：")
    lines.append("  • /添加模型 <provider> key:<你的key>  — 拉取并添加模型（支持所有 provider）")
    lines.append("  • /添加模型 <provider> key:<key> 免费  — 只添加免费模型")
    lines.append("  • /添加模型 <provider> key:<key> url:<地址> — 手动指定 API 地址")
    lines.append("  • /显示模型 免费  — 只显示免费模型")

    return "\n".join(lines)


# ---- 自然语言触发模型拉取 ----
# 触发动词：用户说这些词时，认为是"拉取/添加模型"意图
_NL_FETCH_VERBS = (
    "拉取", "添加", "刷新", "获取", "导入", "更新", "重新拉", "同步",
    "fetch", "pull", "refresh", "add", "import", "update", "sync",
)
# 模型相关名词（任一命中即认为与模型管理相关）
_NL_MODEL_NOUNS = ("模型", "model", "models", "新模型", "免费模型")


def _try_natural_language_fetch(llm, text: str):
    """检测自然语言是否表达"拉取/添加某 provider 模型"意图。

    返回 (provider, free_only, all_models_flag) 或 None。
    provider 已规范化为 canonical 名（如 商汤→sensenova）。
    只有当：① 命中触发动词 + ② 能识别出 provider + ③ 该 provider 有已配置 key
    三者都满足才返回结果，否则返回 None（让上层显示用法）。
    """
    if not text:
        return None
    t = text.lower()

    # 1) 必须命中触发动词
    if not any(v in t for v in _NL_FETCH_VERBS):
        return None

    # 2) 最好提到"模型"（宽松：动词+provider 也算，避免"商汤拉一下"被漏掉）
    has_model_noun = any(n in t for n in _NL_MODEL_NOUNS)

    # 3) 识别 provider：用 resolver 的别名表
    from models.resolver import _PROVIDER_ALIASES, _extract_provider_hint
    provider = _extract_provider_hint(text)
    if provider is None:
        # 回退：直接扫描别名表，找文本里出现的别名
        for alias in sorted(_PROVIDER_ALIASES.keys(), key=len, reverse=True):
            if alias in t:
                provider = _PROVIDER_ALIASES[alias]
                break
    if provider is None:
        return None

    # 4) 该 provider 必须有已配置的 key（否则没法拉）
    if not llm._has_usable_key(provider):
        return None

    # 5) 解析修饰词
    free_only = ("免费" in t) or ("free" in t)
    all_models_flag = ("全部" in t) or ("all models" in t) or ("所有模型" in t)

    # 没提到模型名词也没关系，只要动词+provider+key 都在，就认为是拉取意图
    # （has_model_noun 只用于评分，不强制）
    return provider, free_only, all_models_flag


async def _fetch_models_with_existing_key(
    llm, provider: str, free_only: bool, all_models_flag: bool,
) -> str:
    """用已配置的 key 拉取指定 provider 的模型列表并加入路由。

    与 _add_models 的区别：不需要用户在消息里带 key，直接复用 llm._api_keys。
    """
    import asyncio as _aio
    import time as _time

    api_key = llm._api_keys.get(provider, "")
    if not api_key:
        return (
            f"❌ provider '{provider}' 没有配置 API key。\n"
            f"请用 /添加模型 {provider} key:<你的key> 来添加。"
        )

    # 确定 base_url
    base_url = llm._provider_base_urls.get(provider, "")
    if not base_url:
        from models.resolver import KNOWN_PROVIDERS
        base_url = KNOWN_PROVIDERS.get(provider, "")
        if base_url:
            llm._provider_base_urls[provider] = base_url

    if not base_url:
        return (
            f"❌ 找不到 provider '{provider}' 的 API 地址。\n"
            f"请用 /添加模型 {provider} key:{api_key[:8]}... url:<API地址> 手动指定。"
        )

    # 拉取模型列表
    from models.catalog import ModelCatalog
    cat = ModelCatalog(base_url=base_url, api_key=api_key, provider=provider)
    try:
        n = await cat.refresh(force=True)
    except Exception as exc:
        await cat.aclose()
        return f"❌ 拉取 {provider} 模型列表失败: {exc}\nbase_url: {base_url}"

    if n == 0:
        await cat.aclose()
        return (
            f"❌ 从 {provider} ({base_url}) 未获取到任何模型。\n"
            f"请检查 API Key 是否正确，或 provider 是否有可用模型。"
        )

    all_models_list = cat.all()

    # 筛选
    if all_models_flag:
        filtered = all_models_list
        filter_desc = "全部"
    elif free_only:
        filtered = [m for m in all_models_list if m.is_free]
        filter_desc = "免费"
    else:
        # 自然语言默认拉全部（用户说"新增加的模型"通常想看全貌）
        filtered = all_models_list
        filter_desc = "全部（默认）"

    if not filtered:
        filtered = all_models_list
        filter_desc = "全部（筛选无结果）"

    await cat.aclose()

    # 展示
    lines = [
        f"✅ 从 {provider} 拉取到 {n} 个模型（{filter_desc}筛选后 {len(filtered)} 个）",
        f"   API 地址: {base_url}",
        "",
        "📋 模型列表（已按能力自动分类到 4 层路由）：",
        "",
    ]

    for tier in _TIER_ORDER:
        tier_models = [m for m in filtered if m.tier == tier]
        if not tier_models:
            continue
        info = _TIER_INFO[tier]
        lines.append(f"{info['icon']} {info['name']} — {info['desc']}")
        for i, m in enumerate(tier_models):
            full_id = f"{provider}/{m.id}" if not m.id.startswith(f"{provider}/") else m.id
            caps = _model_capability_desc(full_id)
            ctx = f"{m.context_length:,}" if m.context_length else "?"
            free_tag = "🆓" if m.is_free else "💰"
            lines.append(f"   {i+1}. {free_tag} {m.id}")
            lines.append(f"      层级: {tier} | 上下文: {ctx} | 能力: {caps}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("💡 这些模型已自动添加到智能路由的对应层级。")
    lines.append("   输入 /显示模型 可查看当前所有层级模型。")

    # 自动执行 rebuild_tiers
    try:
        from models.tiers import MODEL_TIERS
        for m in filtered:
            full_id = f"{provider}/{m.id}" if not m.id.startswith(f"{provider}/") else m.id
            tier_list = MODEL_TIERS.setdefault(m.tier, [])
            if full_id not in tier_list:
                tier_list.append(full_id)
        logger.info("自然语言拉取：已把 %d 个 %s 模型加入 MODEL_TIERS",
                    len(filtered), provider)
    except Exception as exc:
        logger.warning("自然语言拉取：加入 MODEL_TIERS 失败: %s", exc)

    return "\n".join(lines)


async def _add_models(llm, input_text: str) -> str:
    """解析 provider + key，拉取模型列表，让用户选择后加入路由。

    支持所有 provider，包括未知 provider（自动探测或 web 搜索）。
    用法:
      /添加模型 nvidia key:nvapi-xxx              — 已知 provider
      /添加模型 英伟达 key:nvapi-xxx 免费           — 中文别名 + 只拉免费
      /添加模型 myprovider key:xxx url:https://... — 手动指定 base_url
      /添加模型 someai key:xxx                    — 未知 provider，自动探测+搜索
    """
    import re as _re
    import asyncio as _aio
    import time as _time

    text = input_text.strip()

    # 提取 key
    key_match = _re.search(r'key[:\s]+(\S+)', text, _re.IGNORECASE)
    if not key_match:
        return _add_models_usage()
    api_key = key_match.group(1)

    # 提取 url（可选）
    url_match = _re.search(r'url[:\s]+(https?://\S+)', text, _re.IGNORECASE)
    explicit_url = url_match.group(1).rstrip(",;") if url_match else ""

    # 移除 key 和 url 部分，剩下的用于解析 provider
    # 注意：两个 match 的索引都是相对于原始 text 的，不能依次切片
    spans = []
    if key_match:
        spans.append((key_match.start(), key_match.end()))
    if url_match:
        spans.append((url_match.start(), url_match.end()))
    spans.sort()
    remaining = ""
    prev_end = 0
    for start, end in spans:
        remaining += text[prev_end:start]
        prev_end = end
    remaining += text[prev_end:]
    remaining = remaining.strip()

    # 检查是否要求免费/全部（用词边界匹配，避免破坏 provider 名称）
    free_only = "免费" in remaining or bool(_re.search(r'\bfree\b', remaining, _re.IGNORECASE))
    all_models_flag = "全部" in remaining or bool(_re.search(r'\ball\b', remaining, _re.IGNORECASE))
    # 移除这些修饰词
    remaining = _re.sub(r'免费|\bfree\b', '', remaining, flags=_re.IGNORECASE).strip()
    remaining = _re.sub(r'全部|\ball\b', '', remaining, flags=_re.IGNORECASE).strip()

    provider = remaining.lower().strip()
    if not provider:
        return _add_models_usage()

    # 别名转换
    from models.resolver import _PROVIDER_ALIASES, KNOWN_PROVIDERS
    if provider in _PROVIDER_ALIASES:
        provider = _PROVIDER_ALIASES[provider]

    # ---- 确定 base_url ----
    base_url = ""

    # 1. 用户显式指定 url
    if explicit_url:
        base_url = explicit_url
        llm._provider_base_urls[provider] = base_url

    # 2. 已知 provider（在 KNOWN_PROVIDERS 里）
    elif provider in llm._provider_base_urls:
        base_url = llm._provider_base_urls[provider]

    elif provider in KNOWN_PROVIDERS:
        base_url = KNOWN_PROVIDERS[provider]
        llm._provider_base_urls[provider] = base_url

    # 3. 未知 provider — 先用 resolver 探测
    else:
        # 注册 API key 触发探测
        llm.set_api_key(provider, api_key)

        # 等待探测完成（最多 15 秒）
        deadline = _time.time() + 15
        while provider not in llm._provider_base_urls and _time.time() < deadline:
            await _aio.sleep(0.5)

        if provider in llm._provider_base_urls:
            base_url = llm._provider_base_urls[provider]
        else:
            # 4. resolver 探测失败 — web 搜索
            base_url = await _search_provider_url(provider, api_key)

            if base_url:
                llm._provider_base_urls[provider] = base_url
            else:
                return (
                    f"❌ 无法找到 provider '{provider}' 的 API 地址。\n\n"
                    f"已尝试：\n"
                    f"  1. 查询已知 provider 列表 — 未找到\n"
                    f"  2. 自动探测常见域名 — 失败\n"
                    f"  3. 网络搜索 — 未找到\n\n"
                    f"请手动指定 API 地址：\n"
                    f"  /添加模型 {provider} key:{api_key[:8]}... url:https://api.example.com/v1"
                )

    # 注册 API key（如果还没注册）
    if provider not in llm._api_keys:
        llm.set_api_key(provider, api_key)
    else:
        llm._api_keys[provider] = api_key

    # ---- 拉取模型列表 ----
    from models.catalog import ModelCatalog
    cat = ModelCatalog(base_url=base_url, api_key=api_key, provider=provider)
    try:
        n = await cat.refresh(force=True)
    except Exception as exc:
        await cat.aclose()
        return f"❌ 拉取模型列表失败: {exc}\nbase_url: {base_url}"

    if n == 0:
        await cat.aclose()
        return (
            f"❌ 从 {provider} ({base_url}) 未获取到任何模型。\n"
            f"请检查 API Key 是否正确。"
        )

    all_models_list = cat.all()

    # 筛选
    if free_only:
        filtered = [m for m in all_models_list if m.is_free]
        filter_desc = "免费"
    elif all_models_flag:
        filtered = all_models_list
        filter_desc = "全部"
    else:
        # 默认只显示免费模型
        filtered = [m for m in all_models_list if m.is_free]
        filter_desc = "免费（默认）"

    if not filtered:
        # 如果筛免费但没结果，显示全部
        filtered = all_models_list
        filter_desc = "全部（未找到免费模型）"

    await cat.aclose()

    # 展示模型列表
    lines = [
        f"✅ 从 {provider} 拉取到 {n} 个模型（{filter_desc}筛选后 {len(filtered)} 个）",
        f"   API 地址: {base_url}",
        "",
        "📋 模型列表（已按能力自动分类到 4 层路由）：",
        "",
    ]

    # 按层级分组展示
    for tier in _TIER_ORDER:
        tier_models = [m for m in filtered if m.tier == tier]
        if not tier_models:
            continue
        info = _TIER_INFO[tier]
        lines.append(f"{info['icon']} {info['name']} — {info['desc']}")
        for i, m in enumerate(tier_models):
            full_id = f"{provider}/{m.id}" if not m.id.startswith(f"{provider}/") else m.id
            caps = _model_capability_desc(full_id)
            ctx = f"{m.context_length:,}" if m.context_length else "?"
            free_tag = "🆓" if m.is_free else "💰"
            lines.append(f"   {i+1}. {free_tag} {m.id}")
            lines.append(f"      层级: {tier} | 上下文: {ctx} | 能力: {caps}")
        lines.append("")

    lines.append("=" * 50)
    lines.append("💡 这些模型已自动添加到智能路由的对应层级。")
    lines.append("   输入 /显示模型 可查看当前所有层级模型。")
    lines.append("   输入 /设置 主模型 <provider/model> 可切换默认模型。")

    # 自动执行 rebuild_tiers 把模型加入 MODEL_TIERS
    try:
        result = await llm.rebuild_tiers(provider=provider, max_per_tier=0, persist=True)
        if not result.get("ok"):
            lines.append(f"\n⚠️ 自动分类时出错: {result.get('error', 'unknown')}")
    except Exception as exc:
        lines.append(f"\n⚠️ 自动分类异常: {exc}")

    return "\n".join(lines)


def _add_models_usage() -> str:
    """返回 /添加模型 的用法说明，包含已知 provider 列表。"""
    from models.resolver import KNOWN_PROVIDERS

    # 按类别分组
    cn_providers = []
    us_providers = []
    other_providers = []
    cn_keys = {"sensenova", "deepseek", "qwen", "glm", "kimi", "yi", "doubao",
               "hunyuan", "spark", "wenxin", "baichuan", "stepfun", "minimax"}
    for name in sorted(KNOWN_PROVIDERS.keys()):
        if name in cn_keys:
            cn_providers.append(name)
        elif name in ("ollama",):
            other_providers.append(name)
        else:
            us_providers.append(name)

    lines = [
        "📡 /添加模型 — 从任意 provider 拉取模型并加入智能路由",
        "",
        "用法:",
        "  /添加模型 <provider> key:<你的key>            — 已知 provider，默认拉免费模型",
        "  /添加模型 <provider> key:<key> 免费            — 只拉免费模型",
        "  /添加模型 <provider> key:<key> 全部            — 拉取所有模型",
        "  /添加模型 <provider> key:<key> url:<地址>      — 手动指定 API 地址",
        "  /添加模型 <未知provider> key:<key>            — 自动探测+网络搜索",
        "",
        "已知 provider 列表:",
    ]
    if cn_providers:
        lines.append(f"  🇨🇳 国内: {', '.join(cn_providers)}")
    if us_providers:
        lines.append(f"  🌍 国际: {', '.join(us_providers)}")
    if other_providers:
        lines.append(f"  💻 其他: {', '.join(other_providers)}")
    lines += [
        "",
        "示例:",
        "  /添加模型 nvidia key:nvapi-xxxxx",
        "  /添加模型 英伟达 key:nvapi-xxxxx 免费",
        "  /添加模型 deepseek key:sk-xxxxx 全部",
        "  /添加模型 openai key:sk-xxxxx",
        "  /添加模型 自定义AI key:xxx url:https://api.myai.com/v1",
    ]
    return "\n".join(lines)


async def _search_provider_url(provider: str, api_key: str) -> str:
    """通过网络搜索查找未知 provider 的 API base_url.

    注意：resolver 的 set_api_key 已经做过域名探测，这里只做 web 搜索。
    如果 web 搜索找到候选域名，再验证 /v1/models 端点是否可用。

    安全修复：必须把 api_key 透传给 _web_search_provider_api，否则探测
    /models 端点时不带 Authorization 头，所有需要认证的 provider
    都会返回 401/403 而被误判为「不可用」。
    """
    # Web 搜索
    try:
        url = await _web_search_provider_api(provider, api_key=api_key)
        if url:
            return url
    except Exception as exc:
        logger.debug("ignored non-critical error: %s", exc)

    return ""


async def _web_search_provider_api(provider: str, api_key: str = "") -> str:
    """通过 web 搜索查找 provider 的 API base_url。

    api_key 用于探测 /models 端点时携带 Authorization 头——绝大多数
    OpenAI 兼容 provider 的 /models 端点都需要认证，不带 key 会全部
    返回 401/403，导致 _web_search_provider_api 永远返回空串。
    """
    import httpx
    import re as _re

    # 探测端点时统一带 Authorization 头（若 api_key 非空）
    auth_headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # 构造搜索查询
    query = f"{provider} API documentation base_url openai compatible endpoint"

    # 尝试 DuckDuckGo Instant Answer API（无需 key）
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            resp = await cli.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            )
            if resp.status_code == 200:
                data = resp.json()
                # 从 AbstractURL 或 RelatedTopics 中提取 URL
                abstract_url = data.get("AbstractURL", "")
                if abstract_url:
                    # 尝试从 abstract URL 推断 API 地址
                    # 例如 https://docs.example.com/api → https://api.example.com/v1
                    m = _re.search(r'https?://(?:docs?\.|api\.)?([\w.-]+)', abstract_url)
                    if m:
                        domain = m.group(1)
                        # 尝试几个常见模式
                        guess_urls = [
                            f"https://api.{domain}/v1",
                            f"https://{domain}/v1",
                            f"https://api.{domain}/api/v1",
                        ]
                        for guess in guess_urls:
                            try:
                                r = await cli.get(
                                    f"{guess}/models",
                                    headers=auth_headers,
                                    timeout=5.0,
                                )
                                if 200 <= r.status_code < 300:
                                    return guess
                            except Exception:
                                continue

                # 搜索 RelatedTopics
                for topic in data.get("RelatedTopics", [])[:5]:
                    topic_url = topic.get("FirstURL", "") or topic.get("Text", "")
                    if topic_url:
                        m = _re.search(r'https?://(?:docs?\.|api\.)?([\w.-]+)', topic_url)
                        if m:
                            domain = m.group(1)
                            guess_urls = [
                                f"https://api.{domain}/v1",
                                f"https://{domain}/v1",
                            ]
                            for guess in guess_urls:
                                try:
                                    r = await cli.get(
                                        f"{guess}/models",
                                        headers=auth_headers,
                                        timeout=5.0,
                                    )
                                    if 200 <= r.status_code < 300:
                                        return guess
                                except Exception:
                                    continue
    except Exception as exc:
        logger.debug("ignored non-critical error: %s", exc)

    return ""
