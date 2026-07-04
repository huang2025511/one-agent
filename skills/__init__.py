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
    ) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.schema = schema
        self.handler = handler
        self.directory = directory
        self.version = version  # Semantic version (e.g., "1.2.3")
        self.changelog = changelog or []  # List of version notes
        self.uses = 0
        self.last_used: Optional[float] = None

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
        for d in (self._builtin_dir, self._user_dir, self._community_dir, self._marketplace_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        self._seed_builtins()
        self._scan_directory(self._builtin_dir)
        self._scan_directory(self._user_dir)
        self._scan_directory(self._community_dir)
        self._scan_directory(self._marketplace_dir)
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

    def get(self, id: str) -> Optional[Skill]:
        return self._skills.get(id)

    def pick_relevant(self, text: str, limit: int = 4) -> List[Skill]:
        """Simple keyword relevance — pick N skills whose title/description
        contain words from the user query.  This avoids loading the entire
        skill catalog into the LLM context.
        """
        query_words = set(w.lower() for w in re.findall(r"\w{3,}", text))
        scored: List[tuple] = []
        for skill in self._skills.values():
            hay = f"{skill.title} {skill.description}".lower()
            hits = sum(1 for w in query_words if w in hay)
            if hits > 0:
                scored.append((hits, skill.title, skill))
        scored.sort(reverse=True)
        return [s[2] for s in scored[:limit]]

    async def dispatch(self, skill_id: str, args: Dict[str, Any]) -> str:
        skill = self._skills.get(skill_id)
        if skill is None:
            return f"[unknown skill: {skill_id}]"
        return await skill.run(args)

    async def _on_cron(self, event: Event) -> None:
        """Handle skill_pattern_mining: re-scan skill directories for new skills."""
        job_name = event.get("name") or ""
        if job_name == "skill_pattern_mining":
            self._scan_directory(self._builtin_dir)
            self._scan_directory(self._user_dir)
            self._scan_directory(self._community_dir)
            self._scan_directory(self._marketplace_dir)
            logger.info("skill pattern mining: %d skills loaded", len(self._skills))

    def register(self, skill: Skill) -> None:
        self._skills[skill.id] = skill
        logger.info("registered skill: %s", skill.id)

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
                ("🔄 /update /更新", "从 GitHub 更新到最新版本"),
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
            except Exception:
                pass
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
            except Exception:
                pass

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
                try:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception:
                    logger.exception("restart: os.execv failed")

            import asyncio as _aio
            try:
                # 修复：用 get_running_loop() 替代 get_event_loop()。
                # restart handler 是同步函数，但被 Coordinator 在 async
                # 上下文里调用。get_event_loop() 在 Python 3.12+ 同步
                # 上下文里 DeprecationWarning；只有在 running loop 里
                # create_task 才有意义，没 running loop 直接 fallback
                # 到 os.execv。
                loop = _aio.get_running_loop()
                # 用 create_task 调度 async 清理 + execv
                loop.create_task(_do_restart_async())
            except RuntimeError:
                # 没 running loop，直接同步执行（跳过清理作为最后兜底）
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
            """执行系统命令（带密码保护）。使用方式: /shell ls -la [--password xxx]"""
            executor = await self._get_system_executor()
            command = str(args.get("command", "")).strip()
            password = str(args.get("password", "")) if args.get("password") else ""

            if not command:
                return "用法: /shell <命令> [--password <密码>]\n示例:\n  /shell ls -la\n  /shell ls -la --password mypass123"

            try:
                result = await executor.dispatch("system.run", {
                    "command": command,
                    "password": password,
                })
            except Exception as exc:
                return f"执行错误: {exc}"

            if not isinstance(result, dict):
                return str(result)

            ok = result.get("ok", False)
            needs_pwd = result.get("requires_password", False)
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

            if needs_pwd:
                return "🔒 需要密码才能执行此命令。\n请使用: /shell " + command + " --password <你的密码>\n或先解锁: /unlock <你的密码>\n\n密码设置:\n  修改 config/default_config.yaml 中的 security.system_executor_password\n  设为空表示允许所有 Level 0 安全命令，其他命令一律拒绝。"

            return f"❌ 执行失败: {err}"

        self.register(Skill(
            id="system_run", title="系统命令",
            description="/shell 或 /sh：执行系统命令。危险操作需要密码。\n安全命令(ls/cat/echo/date 等)免密码，其他命令需密码验证(60分钟缓存)。",
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的系统命令"},
                    "password": {"type": "string", "description": "密码(可选，用于解锁会话)"},
                },
            },
            handler=system_run_handler,
        ))

        # ---------- 解锁会话技能 ----------
        async def system_unlock_handler(args: Dict[str, Any]) -> str:
            """输入密码解锁会话。使用方式: /unlock 密码"""
            executor = await self._get_system_executor()
            password = str(args.get("password", ""))

            if not password:
                return "用法: /unlock <你的密码>\n\n密码设置:\n  修改 config/default_config.yaml 中 security.system_executor_password = \"hash值\"\n  hash值生成: python -c \"from executors.system import SystemExecutor; print(SystemExecutor.hash_password('你的密码'))\""

            try:
                ok = await executor.verify_password(password)
                if ok:
                    return "✅ 解锁成功！60 分钟内执行危险命令不需要再次输入密码。"
                else:
                    return "❌ 密码错误，请重试。(连续3次错误会锁定5分钟)"
            except Exception as exc:
                return f"解锁错误: {exc}"

        self.register(Skill(
            id="system_unlock", title="解锁系统",
            description="/unlock 或 /解锁：输入密码解锁会话(60分钟有效)",
            schema={
                "type": "object",
                "properties": {
                    "password": {"type": "string", "description": "密码"},
                },
            },
            handler=system_unlock_handler,
        ))

        # ---------- 锁定会话技能 ----------
        async def system_lock_handler(args: Dict[str, Any]) -> str:
            """撤销密码缓存，立即锁定。使用方式: /lock"""
            executor = await self._get_system_executor()
            try:
                executor.invalidate_password()
                return "🔒 已锁定。再次执行危险命令需要重新输入密码。"
            except Exception as exc:
                return f"锁定错误: {exc}"

        self.register(Skill(
            id="system_lock", title="锁定系统",
            description="/lock 或 /锁定：撤销密码授权，再次执行危险命令需要重新输入密码",
            schema={
                "type": "object",
                "properties": {},
            },
            handler=system_lock_handler,
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
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "分支名称，默认 main",
                        "default": "main"
                    }
                }
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
                "type": "object",
                "properties": {}
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
                "type": "object",
                "properties": {}
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

            result = _process_settings_command(input_text, config)
            return result

        self.register(Skill(
            id="settings", title="Settings Manager",
            description="/settings 或 /设置：读取或修改 Agent 配置（模型、温度、网关开关等）",
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
            """搜索互联网获取最新信息。多源自动切换：DuckDuckGo → Bing → 自给。

            不依赖任何 API key，纯 HTML 解析。任一源成功即返回结果。
            """
            query = str(args.get("input", "")).strip()
            if not query:
                return "[web_search error: empty query]"

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

            async def _try_ddg() -> bool:
                """DuckDuckGo Lite — clean HTML, no JS."""
                nonlocal results
                try:
                    async with _httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                        resp = await client.post(
                            "https://lite.duckduckgo.com/lite/",
                            data={"q": query, "kl": "cn-zh"},
                            headers=headers,
                        )
                        if resp.status_code != 200:
                            return False
                        html = resp.text
                    for m in _DDG_RESULT_PATTERN.finditer(html):
                        url_r = m.group(1)
                        title = _re.sub(r"<[^>]+>", "", m.group(2)).strip()
                        snippet = _re.sub(r"<[^>]+>", "", m.group(3)).strip()
                        if title and snippet:
                            results.append(f"{title}\n  {snippet}\n  {url_r}")
                    if not results:
                        for m in _DDG_LINK_PATTERN.finditer(html):
                            url_r = m.group(1)
                            title = _re.sub(r"<[^>]+>", "", m.group(2)).strip()
                            if title and "duckduckgo" not in url_r.lower():
                                results.append(f"{title}\n  {url_r}")
                    return bool(results)
                except (_httpx.HTTPStatusError, _httpx.RequestError, _httpx.TimeoutException):
                    return False

            async def _try_bing() -> bool:
                """Bing HTML search — broader coverage."""
                nonlocal results
                try:
                    bing_url = f"https://www.bing.com/search?q={_url_quote(query)}&setlang=zh-cn"
                    async with _httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                        resp = await client.get(bing_url, headers=headers)
                        if resp.status_code != 200:
                            return False
                        html = resp.text
                    # Bing: results are in <li class="b_algo"> blocks
                    for block in _BING_ALGO_PATTERN.finditer(html):
                        block_html = block.group(1)
                        link_m = _re.search(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', block_html, _re.DOTALL)
                        snippet_m = _re.search(r'<p[^>]*>(.*?)</p>', block_html, _re.DOTALL)
                        if link_m:
                            title = _re.sub(r"<[^>]+>", "", link_m.group(2)).strip()
                            url_r = link_m.group(1)
                            snippet = _re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""
                            if title and "bing.com" not in url_r.lower():
                                results.append(f"{title}\n  {snippet}\n  {url_r}")
                    return bool(results)
                except (_httpx.HTTPStatusError, _httpx.RequestError, _httpx.TimeoutException):
                    return False

            # Try sources in order
            for name, fn in [("DuckDuckGo", _try_ddg), ("Bing", _try_bing)]:
                sources_tried.append(name)
                if await fn():
                    break

            if results:
                summary = "\n\n".join(results[:5])
                return f"搜索结果（{query}，来源: {' → '.join(sources_tried)}）：\n\n{summary}"
            return f"[web_search: {'、'.join(sources_tried)} 均无法访问。建议直接基于已有知识回答。]"

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
                        },
                        "required": ["input"],
                    },
                },
            },
            handler=web_search_handler,
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
            description="在沙箱环境中执行 Python 代码，用于数学计算、数据处理、文件操作等。"
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
            # 如果没有 key 参数，显示用法
            import re as _re
            if not _re.search(r'key[:\s]+\S+', input_text, _re.IGNORECASE):
                return _add_models_usage()

            return await _add_models(llm, input_text)

        self.register(Skill(
            id="model_manage", title="Model Manager",
            description="/添加模型 或 /显示模型：管理智能路由模型",
            schema=_schema("model_manage", "manage models and routing", []),
            handler=model_manage_handler,
        ))

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


def _process_settings_command(input_text: str, config: dict) -> str:
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
    _save_config(config)

    return f"已将 {matched_alias} 修改为 {new_value}"


def _save_config(config: dict) -> None:
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
        finally:
            if use_fcntl and lock_fd:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
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
    except Exception:
        pass

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
    except Exception:
        pass

    return ""
