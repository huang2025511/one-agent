"""Skill system — combines OpenClaw's MCP skills + OpenSquilla's MetaSkill.

A *Skill* is either:
    * a plain Markdown file (SKILL.md) describing a procedure, or
    * a MetaSkill (skill.yaml + markdown) that composes atomic skills into
      repeatable multi-step workflows, or
    * an MCP server (Model Context Protocol) where tools are dynamic.

The SkillManager exposes them uniformly as tools consumable by the LLM.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)


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
        for d in (self._builtin_dir, self._user_dir, self._community_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        self._seed_builtins()
        self._scan_directory(self._builtin_dir)
        self._scan_directory(self._user_dir)
        self._scan_directory(self._community_dir)
        # MCP server list — declared in config; started lazily
        self._mcp_servers = cfg.get("mcp_servers", []) or []
        self._max_loaded_per_turn = cfg.get("max_skills_per_turn", self._max_loaded_per_turn)
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
        except Exception:
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
                resolved = command.replace("{input}", input_text)
                try:
                    out = subprocess.run(
                        resolved, shell=True, capture_output=True, text=True, timeout=30
                    )
                    return (out.stdout or "") + (out.stderr or "")
                except subprocess.TimeoutExpired:
                    return "[timeout]"
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
            expr = str(args.get("input", "")).strip()
            if not re.fullmatch(r"[0-9+\-*/(). ]+", expr):
                return "[invalid math expression]"
            try:
                return str(eval(expr, {"__builtins__": {}}, {}))
            except Exception as exc:  # noqa: BLE001
                return f"[math error: {exc}]"
        self.register(Skill(
            id="calc", title="Calculator",
            description="Evaluate a simple arithmetic expression (numbers +-*/).",
            schema=_schema("calc", "evaluate arithmetic expression", ["input"]),
            handler=calc_handler,
        ))

        async def save_note(args: Dict[str, Any]) -> str:
            text = str(args.get("input", ""))
            target = Path(self._builtin_dir or "./data/skills/builtin") / "user_notes.log"
            target.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            target.write_text(target.read_text(encoding="utf-8", errors="ignore") + f"\n[{ts}] {text}\n",
                              encoding="utf-8")
            return f"saved note ({len(text)} chars) to {target}"
        self.register(Skill(
            id="save_note", title="Save note",
            description="Append a note to a persistent log file.",
            schema=_schema("save_note", "append persistent note", ["input"]),
            handler=save_note,
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
