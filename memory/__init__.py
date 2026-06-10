"""Three-tier memory — short / long / procedural (Hermes-style).

Enhanced with:
  - Paginated FTS5 search
  - Memory weight decay for old entries
  - Configurable relevance threshold
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
    """FTS5-backed store with pagination and weight decay."""

    def __init__(self, path: str, decay_enabled: bool = True, decay_factor: float = 0.95) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._decay_enabled = decay_enabled
        self._decay_factor = decay_factor
        self._init_schema()

    def _init_schema(self) -> None:
        c = self._conn.cursor()
        try:
            c.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory USING fts5("
                "content, source, tags, timestamp UNINDEXED)"
            )
        except sqlite3.OperationalError:
            c.execute(
                "CREATE TABLE IF NOT EXISTS memory ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "content TEXT, source TEXT, tags TEXT, timestamp REAL, weight REAL DEFAULT 1.0)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_ts ON memory(timestamp)")
        self._conn.commit()

    def add(self, content: str, source: str = "user", tags: str = "", weight: float = 1.0) -> None:
        c = self._conn.cursor()
        # Check if weight column exists
        c.execute("PRAGMA table_info(memory)")
        columns = {row[1] for row in c.fetchall()}
        if "weight" in columns:
            c.execute(
                "INSERT INTO memory(content, source, tags, timestamp, weight) VALUES (?,?,?,?,?)",
                (content, source, tags, time.time(), weight),
            )
        else:
            c.execute(
                "INSERT INTO memory(content, source, tags, timestamp) VALUES (?,?,?,?)",
                (content, source, tags, time.time()),
            )
        self._conn.commit()

    def search(
        self,
        query: str,
        limit: int = 5,
        offset: int = 0,
        relevance_threshold: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Paginated FTS5 search with optional decay weighting."""
        c = self._conn.cursor()
        try:
            # FTS5 rank(): bm25 returns negative values; more negative = more relevant.
            # We drop the weight subquery because FTS5 virtual tables have no weight column.
            c.execute(
                "SELECT content, source, tags, timestamp, rank "
                "FROM memory WHERE memory MATCH ? "
                "ORDER BY rank LIMIT ? OFFSET ?",
                (query, limit, offset),
            )
            rows = c.fetchall()
            # Normalize: (content, source, tags, timestamp, rank, weight=1.0)
            normalized = [(r[0], r[1], r[2], r[3], r[4], 1.0) for r in rows]
        except sqlite3.OperationalError:
            c.execute(
                "SELECT content, source, tags, timestamp FROM memory "
                "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = c.fetchall()
            # normalize rows: (content, source, tags, timestamp, rank, weight)
            normalized = []
            for r in rows:
                content, source, tags, timestamp = r[0], r[1], r[2], r[3]
                rank = r[4] if len(r) > 4 else 0
                weight = r[5] if len(r) > 5 else 1.0
                normalized.append((content, source, tags, timestamp, rank, weight))

        results = []
        for row in normalized:
            content, source, tags, timestamp = row[0], row[1], row[2], row[3]
            rank = row[4]
            w = row[5] if len(row) > 5 else 1.0
            # Apply decay to old entries
            if self._decay_enabled:
                age_hours = (time.time() - timestamp) / 3600
                w *= self._decay_factor ** min(age_hours / 24, 30)  # cap at 30 days
            # FTS5 bm25 rank is negative (more negative = more relevant).
            # A match always has rank < 0; filter by negative threshold.
            if rank < 0 or not query:
                results.append({
                    "content": content,
                    "source": source,
                    "tags": tags,
                    "timestamp": timestamp,
                    "weight": w,
                })
        return results

    def paginate(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Return paginated list of all memories."""
        c = self._conn.cursor()
        c.execute("SELECT COUNT(*) FROM memory")
        total = c.fetchone()[0]
        offset = (page - 1) * page_size
        c.execute(
            "SELECT content, source, tags, timestamp FROM memory "
            "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        items = [{"content": r[0], "source": r[1], "tags": r[2], "timestamp": r[3]} for r in c.fetchall()]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def stats(self) -> Dict[str, Any]:
        c = self._conn.cursor()
        try:
            c.execute("SELECT COUNT(*), AVG(weight) FROM memory")
            row = c.fetchone()
            return {"rows": row[0] or 0, "avg_weight": round(row[1] or 1.0, 3)}
        except sqlite3.OperationalError:
            c.execute("SELECT COUNT(*) FROM memory")
            return {"rows": c.fetchone()[0] or 0, "avg_weight": 1.0}

    def vacuum(self) -> None:
        self._conn.execute("VACUUM")

    def close(self) -> None:
        """Close the SQLite connection (called from MemoryPlugin.stop)."""
        try:
            self._conn.close()
        except Exception:
            pass


# ---------- tier 3: procedural memory (auto-generated skills) --------------
class ProceduralMemory:
    """Reusable SKILL.md documents — Hermes-style self-growth."""

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
    """Memory orchestrator — hooks into the event bus."""

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

        mem_cfg = cfg.get("long_term", {}) or {}
        self._long = LongTermMemory(
            path=os.path.join(data_dir, "memory/longterm.sqlite"),
            decay_enabled=mem_cfg.get("decay_enabled", True),
        )
        self._procedural = ProceduralMemory(os.path.join(data_dir, "memory/skills"))
        self._max_results = mem_cfg.get("max_results", 5)
        self._auto_create_skills = cfg.get("procedural", {}).get("auto_create_skills", True)
        self._min_usage = cfg.get("procedural", {}).get("min_usage_before_skill", 3)

        self.bus.subscribe("user_message", self._on_user_message)
        self.bus.subscribe("turn_completed", self._on_turn_completed)
        logger.info("memory plugin ready: long_term=%s", self._long.stats())

    async def _on_user_message(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or self._long is None:
            return
        cfg = self.ctx.config.get("memory", {}) if self.ctx else {}
        threshold = (cfg.get("long_term") or {}).get("relevance_threshold", 0.6)
        hits = self._long.search(turn.input_text, limit=self._max_results, relevance_threshold=threshold)
        if hits:
            snippets = "\n".join(f"- {h['content'][:160]}" for h in hits)
            turn.meta["memory_snippets"] = snippets

    async def _on_turn_completed(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or self._long is None:
            return
        if turn.result and not turn.error:
            self._long.add(
                content=f"Q: {turn.input_text}\nA: {turn.result}",
                source=turn.source,
                tags="interaction",
            )
        if self._auto_create_skills and self._procedural and self._looks_teachable(turn):
            triggers = [w for w in re.findall(r"\w{4,}", turn.input_text)][:5]
            if triggers:
                existing = self._procedural.lookup(turn.input_text)
                if existing is None:
                    body = f"# Skill: {triggers[0]}\n\n"
                    body += f"When the user writes something like: *{turn.input_text[:120]}*\n\n"
                    body += "## Tool Plan\n\n```\n" + (turn.result or "")[:2000] + "\n```\n"
                    self._procedural.save(triggers[0], triggers, body)

    def _looks_teachable(self, turn: TurnContext) -> bool:
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

    def search_facts(self, query: str, limit: int = 5, offset: int = 0) -> List[Dict[str, Any]]:
        if self._long is None:
            return []
        return self._long.search(query, limit=limit, offset=offset)

    def stats(self) -> Dict[str, Any]:
        return {
            "long_term": self._long.stats() if self._long else {},
            "procedural_skills": len(self._procedural.list()) if self._procedural else 0,
        }

    async def stop(self) -> None:
        # close SQLite connection to flush WAL and release file locks
        if self._long is not None:
            try:
                self._long.close()
            except Exception:
                pass
        await super().stop()
