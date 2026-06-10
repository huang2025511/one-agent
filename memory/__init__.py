"""Three-tier memory — short / long / procedural (Hermes-style).

Tier 1 — short term: per-turn message list (lives in TurnContext.messages).
Tier 2 — long term: sqlite + FTS5 (or pure-Python fallback), cross-session.
Tier 3 — procedural memory: reusable skill documents created from past
         successful turns — this is how the agent "grows with you".

Design follows Hermes Agent's three-tier memory with OpenClaw's Markdown
storage style for skills.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.context import TurnContext
from core.events import Event
from core.plugin import Plugin

logger = logging.getLogger(__name__)


# ---------- tier 2: long-term memory (cross-session FTS) ------------------
class LongTermMemory:
    """Very small FTS5-backed store.

    We keep the schema minimal so it works without external dependencies.
    """

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        c = self._conn.cursor()
        try:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory USING fts5("
                "content, source, tags, timestamp UNINDEXED)"
            )
        except sqlite3.OperationalError:
            # fallback: plain table if FTS5 is not compiled in
            c.execute(
                "CREATE TABLE IF NOT EXISTS memory ("
                "content TEXT, source TEXT, tags TEXT, timestamp REAL)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_ts ON memory(timestamp)")
        self._conn.commit()

    def add(self, content: str, source: str = "user", tags: str = "") -> None:
        c = self._conn.cursor()
        c.execute(
            "INSERT INTO memory(content, source, tags, timestamp) VALUES (?,?,?,?)",
            (content, source, tags, time.time()),
        )
        self._conn.commit()

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        c = self._conn.cursor()
        try:
            c.execute(
                "SELECT content, source, tags, timestamp FROM memory "
                "WHERE memory MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            )
        except sqlite3.OperationalError:
            # fallback scan
            c.execute(
                "SELECT content, source, tags, timestamp FROM memory "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        return [
            {"content": row[0], "source": row[1], "tags": row[2], "timestamp": row[3]}
            for row in c.fetchall()
        ]

    def stats(self) -> Dict[str, Any]:
        c = self._conn.cursor()
        c.execute("SELECT COUNT(*) FROM memory")
        return {"rows": c.fetchone()[0]}

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")


# ---------- tier 3: procedural memory (auto-generated skills) --------------
class ProceduralMemory:
    """Reusable SKILL.md documents — the Hermes "grows with you" mechanism.

    On turn success, if the pattern matches a known "teachable" shape
    (repeated tool use, long reasoning chain), we write a SKILL.md so next
    time we get the same prompt we can shortcut straight to the tool plan.
    """

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "_index.json"
        self._index: Dict[str, Any] = self._load_index()

    def _load_index(self) -> Dict[str, Any]:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text(encoding="utf-8"))
            except Exception:
                return {"skills": {}}
        return {"skills": {}}

    def save(self, name: str, triggers: List[str], body: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or "skill"
        path = self._dir / f"{safe}.md"
        path.write_text(body, encoding="utf-8")
        self._index["skills"][safe] = {
            "triggers": triggers,
            "path": str(path),
            "created_at": time.time(),
            "uses": 0,
        }
        self._persist_index()
        logger.info("saved skill %s (%d triggers)", safe, len(triggers))

    def lookup(self, text: str) -> Optional[Dict[str, Any]]:
        """Scan trigger phrases; return best matching skill body or None."""
        best: Optional[Dict[str, Any]] = None
        best_hits = 0
        for skill_id, meta in self._index["skills"].items():
            hits = sum(1 for t in meta["triggers"] if t and t.lower() in text.lower())
            if hits > best_hits:
                best_hits = hits
                best = meta
        if best is None or best_hits == 0:
            return None
        body = Path(best["path"]).read_text(encoding="utf-8")
        best["uses"] += 1
        self._persist_index()
        return {"id": skill_id, "body": body, "meta": best}

    def list(self) -> List[str]:
        return list(self._index["skills"].keys())

    def _persist_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2), encoding="utf-8")


# ---------- public plugin --------------------------------------------------
class MemoryPlugin(Plugin):
    """Memory orchestrator — hooks into the bus."""

    name = "memory"

    def __init__(self) -> None:
        super().__init__()
        self._long: Optional[LongTermMemory] = None
        self._procedural: Optional[ProceduralMemory] = None
        self._max_results = 5
        self._auto_create_skills = True
        self._min_usage = 3

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("memory", {}) or {}
        data_dir = ctx.config.get("agent", {}).get("data_dir", "./data")
        self._long = LongTermMemory(os.path.join(data_dir, "memory/longterm.sqlite"))
        self._procedural = ProceduralMemory(os.path.join(data_dir, "memory/skills"))
        self._max_results = (cfg.get("long_term") or {}).get("max_results", 5)
        self._auto_create_skills = (cfg.get("procedural") or {}).get("auto_create_skills", True)
        self._min_usage = (cfg.get("procedural") or {}).get("min_usage_before_skill", 3)

        self.bus.subscribe("user_message", self._on_user_message)
        self.bus.subscribe("turn_completed", self._on_turn_completed)

    # ------------------------------------------------------- handlers
    async def _on_user_message(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or self._long is None:
            return
        hits = self._long.search(turn.input_text, limit=self._max_results)
        if hits:
            snippets = "\n".join(f"- {h['content'][:160]}" for h in hits)
            turn.meta["memory_snippets"] = snippets

    async def _on_turn_completed(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or self._long is None:
            return
        if turn.result and not turn.error:
            # store successful Q&A for future recall
            self._long.add(
                content=f"Q: {turn.input_text}\nA: {turn.result}",
                source=turn.source,
                tags="interaction",
            )
        # procedural memory — auto-create a skill if the turn pattern is
        # "repeatable" and contains structured output we can template.
        if self._auto_create_skills and self._procedural and self._looks_teachable(turn):
            triggers = [w for w in re.findall(r"\w{4,}", turn.input_text)][:5]
            if triggers:
                # only record once; the procedural memory plugin isn't meant
                # to compete with community skills, just automate obvious
                # routines.
                existing = self._procedural.lookup(turn.input_text)
                if existing is None:
                    body = f"# Skill: {triggers[0]}\n\n"
                    body += f"When the user writes something like: "
                    body += f"*{turn.input_text[:120]}*\n\n"
                    body += "## Tool Plan\n\n"
                    body += f"- You can reuse this reply as a template:\n\n"
                    body += "```\n" + (turn.result or "")[:2000] + "\n```\n"
                    self._procedural.save(triggers[0], triggers, body)

    # --------------------------------------------------------- helpers
    def _looks_teachable(self, turn: TurnContext) -> bool:
        """Heuristic: teachable if the reply is medium-long and uses tools
        or contains structured sections (code blocks / bullet lists).
        """
        if not turn.result:
            return False
        if "```" in turn.result and len(turn.result) > 200:
            return True
        if len(re.findall(r"(?m)^\s*[-*]\s+", turn.result)) >= 3:
            return True
        return False

    # --------------------------------------------------------- public
    def add_fact(self, text: str, source: str = "manual", tags: str = "") -> None:
        if self._long is not None:
            self._long.add(text, source=source, tags=tags)

    def search_facts(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if self._long is None:
            return []
        return self._long.search(query, limit=limit)

    def stats(self) -> Dict[str, Any]:
        return {
            "long_term": self._long.stats() if self._long else {},
            "procedural_skills": len(self._procedural.list()) if self._procedural else 0,
        }
