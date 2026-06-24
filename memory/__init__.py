"""Three-tier memory — short / long / procedural (Hermes-style).

Enhanced with:
  - Paginated FTS5 search
  - Memory weight decay for old entries
  - Configurable relevance threshold
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.context import TurnContext
from core.events import Event
from core.plugin import Plugin

from .embeddings import EmbeddingStore  # noqa: F401
from .knowledge_graph import KnowledgeGraph  # noqa: F401
from .session_store import SessionStore  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = [
    "MemoryPlugin",
    "LongTermMemory",
    "ProceduralMemory",
    "KnowledgeGraph",
    "SessionStore",
    "EmbeddingStore",
]


def _escape_fts5_query(query: str) -> str:
    """Escape FTS5 special characters to prevent injection attacks.

    FTS5 special characters: * ? " ( ) : ^ + - AND OR NOT NEAR
    We strip all non-alphanumeric characters (except CJK and spaces)
    to prevent any FTS5 operator injection.
    """
    # Strip all characters except letters, digits, CJK, and whitespace
    # This is the safest approach — no FTS5 operators can survive
    result = re.sub(r'[^\w\s\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]', ' ', query)

    # Collapse multiple spaces
    result = re.sub(r'\s+', ' ', result).strip()

    # Limit query length to prevent DoS
    if len(result) > 500:
        result = result[:500]

    return result


# ---------- tier 2: long-term memory (cross-session FTS) ------------------
class LongTermMemory:
    """FTS5-backed store with pagination and weight decay."""

    def __init__(self, path: str, decay_enabled: bool = True, decay_factor: float = 0.95) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        # Enable WAL so concurrent readers (e.g. /api/memory/page) don't
        # block writers from the event-bus turn handler.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # Set busy timeout to wait up to 5 seconds for locks
            self._conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError:
            pass
        self._decay_enabled = decay_enabled
        self._decay_factor = decay_factor
        self._has_weight_col: Optional[bool] = None
        # Write lock — serializes write operations across threads to
        # prevent "database is locked" errors when multiple asyncio
        # tasks (via asyncio.to_thread) access this connection.
        self._write_lock = threading.RLock()
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

    def add(self, content: str, source: str = "user", tags: str = "", weight: float = 1.0) -> Optional[int]:
        """Add a memory entry with retry logic for transient lock errors.

        Returns the rowid of the inserted entry so callers can associate
        related data (e.g. embeddings) without a separate
        ``last_insert_rowid()`` query, which would be racy in a
        multi-threaded context.
        """
        with self._write_lock:
            # Retry logic for "database is locked" errors
            max_retries = 3
            result_rowid: Optional[int] = None
            for attempt in range(max_retries):
                # Create a fresh cursor inside the loop — closing it in finally
                # and reusing a closed cursor was the original bug.
                c = self._conn.cursor()
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    if self._has_weight_col is None:
                        try:
                            # Use a separate cursor for schema check to avoid state issues
                            schema_cursor = self._conn.cursor()
                            schema_cursor.execute("PRAGMA table_info(memory)")
                            self._has_weight_col = "weight" in {row[1] for row in schema_cursor.fetchall()}
                            schema_cursor.close()
                        except sqlite3.DatabaseError:
                            self._has_weight_col = False
                    if self._has_weight_col:
                        c.execute(
                            "INSERT INTO memory(content, source, tags, timestamp, weight) VALUES (?,?,?,?,?)",
                            (content, source, tags, time.time(), weight),
                        )
                    else:
                        c.execute(
                            "INSERT INTO memory(content, source, tags, timestamp) VALUES (?,?,?,?)",
                            (content, source, tags, time.time()),
                        )
                    result_rowid = c.lastrowid
                    self._conn.commit()
                    break  # Success
                except sqlite3.OperationalError as exc:
                    self._conn.rollback()
                    if "locked" in str(exc).lower() and attempt < max_retries - 1:
                        import time as time_module
                        delay = min(0.01 * (2 ** attempt), 0.1)  # 10ms, 20ms, 40ms
                        logger.warning(
                            "memory add: database locked (attempt %d/%d), retrying in %.0fms: %s",
                            attempt + 1, max_retries, delay * 1000, exc
                        )
                        time_module.sleep(delay)
                        continue
                    # Non-lock error or final attempt
                    logger.exception("memory add failed: %s", exc)
                    raise
                except sqlite3.Error as exc:
                    self._conn.rollback()
                    logger.exception("memory add failed: %s", exc)
                    raise
                finally:
                    c.close()
            return result_rowid

    def search(
        self,
        query: str,
        limit: int = 5,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Paginated FTS5 search with optional decay weighting.

        Note: ``relevance_threshold`` was removed in v2.1 — it was a
        dead parameter (FTS5 bm25 returns negative ranks; the filtering
        is done by passing limit=1 to discover any match).  Callers
        should set ``limit=1`` for a boolean "does it match?" check.
        """
        # Escape FTS5 special characters to prevent injection attacks
        query = _escape_fts5_query(query)

        c = self._conn.cursor()
        try:
            # FTS5 rank(): bm25 returns negative values; more negative = more relevant.
            # Include rowid directly to avoid N+1 queries
            c.execute(
                "SELECT rowid, content, source, tags, timestamp, rank "
                "FROM memory WHERE memory MATCH ? "
                "ORDER BY rank LIMIT ? OFFSET ?",
                (query, limit, offset),
            )
            rows = c.fetchall()
            # Normalize: (rowid, content, source, tags, timestamp, rank, weight=1.0)
            normalized = [(r[0], r[1], r[2], r[3], r[4], r[5], 1.0) for r in rows]
        except sqlite3.OperationalError:
            c.execute(
                "SELECT rowid, content, source, tags, timestamp FROM memory "
                "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            rows = c.fetchall()
            # normalize rows: (rowid, content, source, tags, timestamp, rank=0, weight=1.0)
            normalized = []
            for r in rows:
                rowid, content, source, tags, timestamp = r[0], r[1], r[2], r[3], r[4]
                normalized.append((rowid, content, source, tags, timestamp, 0, 1.0))

        results = []
        for row in normalized:
            rowid, content, source, tags, timestamp = row[0], row[1], row[2], row[3], row[4]
            rank = row[5]
            w = row[6] if len(row) > 6 else 1.0
            # Apply decay to old entries
            if self._decay_enabled:
                # Use monotonic time for decay calculation to avoid wall clock issues
                # Note: timestamp is stored as wall clock, but decay uses relative time
                age_hours = (time.time() - timestamp) / 3600
                # Cap decay at 30 days to prevent underflow
                age_hours = min(age_hours, 30 * 24)
                w *= self._decay_factor ** (age_hours / 24)
            # FTS5 bm25 rank is negative (more negative = more relevant).
            # A match always has rank < 0; filter by negative threshold.
            if rank < 0 or not query:
                results.append({
                    "id": str(rowid),
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
        with self._write_lock:
            self._conn.execute("VACUUM")

    def get_by_id(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Get a memory entry by its rowid."""
        c = self._conn.cursor()
        try:
            c.execute(
                "SELECT rowid, content, source, tags, timestamp FROM memory WHERE rowid = ?",
                (memory_id,),
            )
            row = c.fetchone()
            if row:
                return {
                    "id": str(row[0]),
                    "content": row[1],
                    "source": row[2],
                    "tags": row[3],
                    "timestamp": row[4],
                }
        except sqlite3.Error as exc:
            logger.exception("get_by_id(%s) failed: %s", memory_id, exc)
        return None

    def get_by_ids(self, memory_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Batch-fetch memory entries by rowid.

        Returns a dict keyed by the string rowid. Replaces N+1 loops that
        called ``get_by_id`` once per semantic-search hit.
        """
        if not memory_ids:
            return {}
        c = self._conn.cursor()
        # SQLite parameter limits are high enough for typical top_k (5-10);
        # chunk defensively to stay well below the 999-host-var ceiling.
        results: Dict[str, Dict[str, Any]] = {}
        try:
            for i in range(0, len(memory_ids), 500):
                chunk = memory_ids[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                c.execute(
                    f"SELECT rowid, content, source, tags, timestamp FROM memory WHERE rowid IN ({placeholders})",
                    chunk,
                )
                for row in c.fetchall():
                    results[str(row[0])] = {
                        "id": str(row[0]),
                        "content": row[1],
                        "source": row[2],
                        "tags": row[3],
                        "timestamp": row[4],
                    }
        except sqlite3.Error as exc:
            logger.exception("get_by_ids(%s) failed: %s", memory_ids, exc)
        return results

    def close(self) -> None:
        """Close the SQLite connection (called from MemoryPlugin.stop)."""
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            logger.error("failed to close SQLite connection: %s", exc, exc_info=True)


# ---------- tier 3: procedural memory (auto-generated skills) --------------
class ProceduralMemory:
    """Reusable SKILL.md documents — Hermes-style self-growth."""

    def __init__(self, directory: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "_index.json"
        self._index: Dict[str, Any] = self._load_index()
        # Batch-write hint: only persist to disk after this many dirty
        # mutations.  Keeps the lookup→update→write hot path cheap.
        self._dirty_count = 0
        self._dirty_threshold = 20
        # Thread-safe access: save() is called via asyncio.to_thread while
        # lookup() runs on the event loop thread. Without a lock, concurrent
        # dict mutation during iteration raises RuntimeError.
        self._lock = threading.Lock()

    def _load_index(self) -> Dict[str, Any]:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("failed to load procedural memory index: %s", exc, exc_info=True)
                return {"skills": {}}
        return {"skills": {}}

    def save(self, name: str, triggers: List[str], body: str) -> None:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or "skill"
        path = self._dir / f"{safe}.md"
        path.write_text(body, encoding="utf-8")
        with self._lock:
            self._index["skills"][safe] = {
                "triggers": triggers,
                "path": str(path),
                "created_at": time.time(),
                "uses": 0,
            }
            # Mark dirty — persist happens on the next threshold flush or
            # explicit flush() call.  Saves the JSON write on every save().
            self._dirty_count += 1
            if self._dirty_count >= self._dirty_threshold:
                self._persist_index()
            else:
                logger.info("saved skill %s (%d triggers, %d dirty)",
                            safe, len(triggers), self._dirty_count)

    def lookup(self, text: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            best: Optional[Dict[str, Any]] = None
            best_id: Optional[str] = None
            best_hits = 0
            for skill_id, meta in self._index["skills"].items():
                hits = sum(1 for t in meta["triggers"] if t and t.lower() in text.lower())
                if hits > best_hits:
                    best_hits = hits
                    best = meta
                    best_id = skill_id
            if best is None or best_id is None or best_hits == 0:
                return None
            # Copy meta to avoid mutation outside the lock
            best = dict(best)
            best["uses"] += 1
            # Increment dirty count; only persist on the next flush.
            self._dirty_count += 1
            need_flush = self._dirty_count >= self._dirty_threshold
        if need_flush:
            self._persist_index()
        try:
            body = Path(best["path"]).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as exc:
            logger.warning("procedural memory: skill file missing: %s", exc)
            return None
        return {"id": best_id, "body": body, "meta": best}

    def list(self) -> List[str]:
        return list(self._index["skills"].keys())

    def flush(self) -> None:
        """Force-persist any pending dirty mutations to disk."""
        if self._dirty_count > 0:
            self._persist_index()

    def _persist_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2), encoding="utf-8")
        self._dirty_count = 0


# ---------- public plugin --------------------------------------------------
class MemoryPlugin(Plugin):
    """Memory orchestrator — hooks into the event bus."""

    name = "memory"

    def __init__(self) -> None:
        super().__init__()
        self._long: Optional[LongTermMemory] = None
        self._procedural: Optional[ProceduralMemory] = None
        self._kg: Optional[KnowledgeGraph] = None
        self._embeddings: Optional[EmbeddingStore] = None
        self._max_results = 5
        self._auto_create_skills = True
        self._min_usage = 3
        self._hybrid_search = True  # Enable hybrid search (FTS + embedding)

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
        self._kg = KnowledgeGraph(os.path.join(data_dir, "memory/kg.db"))

        # Initialize embedding store for semantic search
        self._hybrid_search = mem_cfg.get("hybrid_search", True)
        if self._hybrid_search:
            try:
                self._embeddings = EmbeddingStore(
                    db_path=os.path.join(data_dir, "memory/embeddings.db")
                )
                logger.info("Embedding store initialized for hybrid search")
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("failed to initialize embedding store: %s", exc, exc_info=True)
                self._hybrid_search = False

        self._max_results = mem_cfg.get("max_results", 5)
        self._auto_create_skills = cfg.get("procedural", {}).get("auto_create_skills", True)
        self._min_usage = cfg.get("procedural", {}).get("min_usage_before_skill", 3)

        self.bus.subscribe("user_message", self._on_user_message)
        self.bus.subscribe("turn_completed", self._on_turn_completed)
        self.bus.subscribe("cron", self._on_cron)
        logger.info("memory plugin ready: long_term=%s, hybrid_search=%s",
                    self._long.stats(), self._hybrid_search)

    async def _on_user_message(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or self._long is None:
            return

        # FTS5 keyword search — wrap in to_thread to avoid blocking
        # the event loop on SQLite I/O
        fts_hits = await asyncio.to_thread(
            self._long.search, turn.input_text, limit=self._max_results
        )

        # Hybrid search: combine FTS + embedding semantic search
        if self._hybrid_search and self._embeddings:
            try:
                # Get embedding for query — SentenceTransformer.encode()
                # is CPU-bound and must not block the event loop
                query_vec = await asyncio.to_thread(
                    self._embeddings.embed, turn.input_text
                )
                if query_vec is not None:
                    # Semantic search — also offload (may scan all vectors)
                    semantic_results = await asyncio.to_thread(
                        self._embeddings.search, query_vec, top_k=self._max_results
                    )

                    # Merge results: FTS hits + semantic hits
                    # Use memory_id to avoid duplicates
                    seen_ids = set()
                    merged_hits = []

                    # Add FTS hits first (keyword match has higher priority)
                    for hit in fts_hits:
                        memory_id = hit.get("id")
                        if memory_id and memory_id not in seen_ids:
                            seen_ids.add(memory_id)
                            merged_hits.append(hit)

                    # Add semantic results — batch-fetch to avoid N+1 queries
                    new_ids = [mid for mid, _score in semantic_results if mid not in seen_ids]
                    entries_map: Dict[str, Dict[str, Any]] = {}
                    if new_ids:
                        entries_map = await asyncio.to_thread(self._long.get_by_ids, new_ids)
                    for memory_id, score in semantic_results:
                        if memory_id not in seen_ids:
                            seen_ids.add(memory_id)
                            hit = entries_map.get(str(memory_id))
                            if hit:
                                hit["semantic_score"] = score
                                merged_hits.append(hit)

                    hits = merged_hits[:self._max_results]
                else:
                    hits = fts_hits
            except (RuntimeError, ValueError, OSError) as exc:
                logger.error("hybrid search failed, falling back to FTS: %s", exc, exc_info=True)
                hits = fts_hits
        else:
            hits = fts_hits

        if hits:
            snippets = "\n".join(f"- {h['content'][:160]}" for h in hits)
            turn.meta["memory_snippets"] = snippets

    async def _on_turn_completed(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or self._long is None:
            return
        if turn.result and not turn.error:
            content = f"Q: {turn.input_text}\nA: {turn.result}"
            # All operations below are synchronous SQLite / CPU-bound work
            # (embedding encode is especially heavy). Run them in a worker
            # thread to avoid blocking the event loop (mirrors _on_user_message).
            memory_id = await asyncio.to_thread(
                self._long.add,
                content=content,
                source=turn.source,
                tags="interaction",
            )
            # Store embedding vector for semantic search — this was missing,
            # causing hybrid search to degenerate to FTS-only.
            # Use the rowid returned by add() instead of a separate
            # last_insert_rowid() query, which would be racy in a
            # multi-threaded context.
            if self._embeddings is not None and memory_id is not None:
                try:
                    vec = await asyncio.to_thread(self._embeddings.embed, content)
                    if vec is not None:
                        await asyncio.to_thread(
                            self._embeddings.store, str(memory_id), vec)
                except Exception as exc:
                    logger.debug("embedding store failed: %s", exc)
            # Auto-extract entities into knowledge graph
            if self._kg is not None:
                combined = turn.input_text + " " + (turn.result or "")
                await asyncio.to_thread(
                    self._kg.extract_from_text, combined, source=turn.source)
        if self._auto_create_skills and self._procedural and self._looks_teachable(turn):
            triggers = [w for w in re.findall(r"\w{4,}", turn.input_text)][:5]
            if triggers:
                existing = self._procedural.lookup(turn.input_text)
                if existing is None:
                    body = f"# Skill: {triggers[0]}\n\n"
                    body += f"When the user writes something like: *{turn.input_text[:120]}*\n\n"
                    body += "## Tool Plan\n\n```\n" + (turn.result or "")[:2000] + "\n```\n"
                    await asyncio.to_thread(
                        self._procedural.save, triggers[0], triggers, body)

    async def _on_cron(self, event: Event) -> None:
        """Handle scheduled maintenance tasks."""
        job_name = event.get("name") or ""
        if job_name == "memory_housekeeping" and self._long is not None:
            await asyncio.to_thread(self._long.vacuum)
            logger.info("memory housekeeping: vacuum completed, stats=%s", self._long.stats())

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

    def paginate_facts(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Paginated view over the entire long-term store.

        Public wrapper so external callers (REST API, monitor) don't have
        to reach into ``_long`` directly.  Returns
        ``{items, page, page_size, total, total_pages}`` or an empty
        ``items`` list if the store hasn't been initialised.
        """
        if self._long is None:
            return {"items": [], "page": page, "page_size": page_size,
                    "total": 0, "total_pages": 0}
        return self._long.paginate(page=page, page_size=page_size)

    def stats(self) -> Dict[str, Any]:
        return {
            "long_term": self._long.stats() if self._long else {},
            "procedural_skills": len(self._procedural.list()) if self._procedural else 0,
            "knowledge_graph": self._kg.stats() if self._kg else {},
        }

    def graph_search(self, action: str = "search", **kwargs) -> Any:
        """Query the knowledge graph.

        Actions:
          - search: search entities by name (keyword query)
          - entity: get entity details and its relationships
          - neighbors: get entities within N hops
        """
        if self._kg is None:
            return {"error": "knowledge graph not initialized"}
        if action == "search":
            return self._kg.search(kwargs.get("query", ""), limit=kwargs.get("limit", 10))
        elif action == "entity":
            name = kwargs.get("name", "")
            if not name:
                return {"error": "entity name required"}
            result = self._kg.query_entity(name)
            return result if result is not None else {"error": f"entity '{name}' not found"}
        elif action == "neighbors":
            name = kwargs.get("name", "")
            if not name:
                return {"error": "entity name required"}
            return self._kg.get_neighbors(name, depth=kwargs.get("depth", 1))
        else:
            return {"error": f"unknown action: {action}, supported: search, entity, neighbors"}

    async def stop(self) -> None:
        # Flush pending procedural-memory writes (so we don't lose the last
        # ~20 dirty mutations) and close the SQLite connection (flushes
        # WAL and releases file locks).
        if self._procedural is not None:
            try:
                self._procedural.flush()
            except OSError as exc:
                logger.error("failed to flush procedural memory: %s", exc, exc_info=True)
        if self._long is not None:
            try:
                self._long.close()
            except sqlite3.Error as exc:
                logger.error("failed to close long-term memory: %s", exc, exc_info=True)
        if self._kg is not None:
            try:
                self._kg.close()
            except (OSError, RuntimeError) as exc:
                logger.error("failed to close knowledge graph: %s", exc, exc_info=True)
        if self._embeddings is not None:
            try:
                self._embeddings.close()
            except (OSError, RuntimeError) as exc:
                logger.error("failed to close embedding store: %s", exc, exc_info=True)
        await super().stop()
