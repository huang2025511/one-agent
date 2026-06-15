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
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.events import Event
from core.plugin import Plugin

from .document_search import DocumentStore
from multimodal import make_transcribe_handler, make_image_handler
from skills.document_search import make_doc_search_handler
from memory.knowledge_graph import make_graph_search_handler

logger = logging.getLogger(__name__)

# Module-level singleton — shared between skill handler and API
_doc_store = DocumentStore()


class Skill:
    """Single-responsibility skill wrapper.

    The LLM sees it as a JSON-schema tool; the runtime dispatches to
    Python callables or shell commands declared in the skill's header.
    """

    def __init__(self, id: str, title: str, description: str, schema: Dict[str, Any],
                 handler, directory: Optional[str] = None) -> None:
        self.id = id
        self.title = title
        self.description = description
        self.schema = schema
        self.handler = handler
        self.directory = directory
        self.uses = 0
        self.last_used: Optional[float] = None

    async def run(self, args: Dict[str, Any]) -> str:
        self.uses += 1
        self.last_used = time.time()
        try:
            return await self.handler(args)
        except Exception as exc:  # noqa: BLE001
            return f"[skill:{self.id} error] {exc}"


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
            except Exception:
                logger.exception("failed to load %s", path)

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
    def _seed_builtins(self) -> None:
        """Seed built-in skills that are always available.

        These are pure-Python, so they need no subprocess.  They cover the
        agent's most common operational needs: echo, math, timestamp,
        file-cat.
        """
        import datetime

        async def echo_handler(args: Dict[str, Any]) -> str:
            return args.get("input", "")
        self.register(Skill(
            id="echo", title="Echo",
            description="Echo input back to the caller — used as a debug placeholder.",
            schema=_schema("echo", "echo input back", ["input"]),
            handler=echo_handler,
        ))

        async def now_handler(args: Dict[str, Any]) -> str:
            return datetime.datetime.now().isoformat()
        self.register(Skill(
            id="now", title="Current time",
            description="Return the current local timestamp in ISO format.",
            schema=_schema("now", "return current timestamp", []),
            handler=now_handler,
        ))

        async def calc_handler(args: Dict[str, Any]) -> str:
            import ast
            import operator
            expr = str(args.get("input", "")).strip()
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
                if isinstance(node, ast.Num):  # py<3.8 compat
                    return node.n
                if isinstance(node, ast.BinOp):
                    return _ops[type(node.op)](_eval(node.left), _eval(node.right))
                if isinstance(node, ast.UnaryOp):
                    return _ops[type(node.op)](_eval(node.operand))
                raise ValueError(f"unsupported node: {type(node).__name__}")
            try:
                tree = ast.parse(expr, mode="eval")
                return str(_eval(tree.body))
            except Exception as exc:  # noqa: BLE001
                return f"[math error: {exc}]"
        self.register(Skill(
            id="calc", title="Calculator",
            description="Evaluate a simple arithmetic expression (numbers +-*/).",
            schema=_schema("calc", "evaluate arithmetic expression", ["input"]),
            handler=calc_handler,
        ))

        async def save_note(args: Dict[str, Any]) -> str:
            # 跨平台文件锁：fcntl 在 Windows 不可用，降级为无锁
            try:
                import fcntl
                _has_fcntl = True
            except ImportError:
                _has_fcntl = False
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
                else:
                    f.write(f"[{ts}] {text}\n")
            return "note saved"
        self.register(Skill(
            id="save_note", title="Save note",
            description="Append a note to a persistent log file.",
            schema=_schema("save_note", "append persistent note", ["input"]),
            handler=save_note,
        ))

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

        # ---------- 网页搜索技能 ----------
        async def web_search_handler(args: Dict[str, Any]) -> str:
            """搜索互联网获取最新信息。多源自动切换：DuckDuckGo → Bing → 自给。
            
            不依赖任何 API key，纯 HTML 解析。任一源成功即返回结果。
            """
            query = str(args.get("input", "")).strip()
            if not query:
                return "[web_search error: empty query]"
            
            import re as _re
            import httpx as _httpx
            from urllib.parse import quote as _url_quote
            
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
                    pattern = _re.compile(
                        r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<span[^>]*class="[^"]*snippet[^"]*"[^>]*>(.*?)</span>',
                        _re.DOTALL | _re.IGNORECASE,
                    )
                    for m in pattern.finditer(html):
                        url_r = m.group(1)
                        title = _re.sub(r"<[^>]+>", "", m.group(2)).strip()
                        snippet = _re.sub(r"<[^>]+>", "", m.group(3)).strip()
                        if title and snippet:
                            results.append(f"{title}\n  {snippet}\n  {url_r}")
                    if not results:
                        link_pattern = _re.compile(
                            r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', _re.DOTALL,
                        )
                        for m in link_pattern.finditer(html):
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
                    algo_pattern = _re.compile(
                        r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
                        _re.DOTALL | _re.IGNORECASE,
                    )
                    for block in algo_pattern.finditer(html):
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

        self.register(Skill(
            id="settings", title="Settings Manager",
            description="读取或修改 Agent 配置（模型、温度、网关开关等）。"
                        "示例：'查看当前模型'、'把温度改为0.7'、'开启Docker'、'列出所有设置'",
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
            handler=make_doc_search_handler(_doc_store),
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
_SENSITIVE_KEYS = {"rest.api_key", "llm.api_keys"}


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
    current = d
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


def _parse_bool_value(text: str) -> Optional[bool]:
    """从自然语言中解析布尔值。"""
    t = text.lower().strip()
    if t in {"true", "yes", "1", "on", "开", "开启", "启用", "打开", "打开", "是", "enable", "enabled"}:
        return True
    if t in {"false", "no", "0", "off", "关", "关闭", "禁用", "停用", "否", "disable", "disabled"}:
        return False
    return None


def _parse_value(text: str, value_type: type):
    """根据目标类型解析用户输入的值。"""
    text = text.strip().strip("\"'").strip()
    if value_type == bool:
        v = _parse_bool_value(text)
        if v is not None:
            return v
        return None
    if value_type == int:
        import re as _re
        m = _re.search(r"-?\d+", text)
        return int(m.group()) if m else None
    if value_type == float:
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
        return f"未识别的设置项。可设置的选项：{', '.join(a for a in _SETTING_ALIASES if any('\u4e00' <= c <= '\u9fff' for c in a))}"

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
    """将配置写回 YAML 文件（原子写入）。"""
    import yaml
    import tempfile
    config_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
    try:
        # Write to temp file first, then atomically rename
        dir_name = os.path.dirname(config_path) or "."
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".yaml",
            dir=dir_name,
            delete=False,
        ) as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            temp_path = f.name
        # Atomic rename
        os.replace(temp_path, config_path)
    except Exception as exc:
        logger.warning("保存配置失败: %s", exc)
        # Clean up temp file on error
        try:
            if 'temp_path' in locals():
                os.unlink(temp_path)
        except Exception:
            pass