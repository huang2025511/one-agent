"""Knowledge graph memory — entity extraction and relationship queries."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import logging
from typing import Any, Dict, List, Optional, Tuple

from .base_store import BaseSQLiteStore

logger = logging.getLogger(__name__)


class KnowledgeGraph(BaseSQLiteStore):
    """Lightweight entity-relationship graph on SQLite."""

    def __init__(self, db_path: str = "data/memory/kg.db"):
        super().__init__(db_path)

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                type TEXT DEFAULT 'unknown',
                source TEXT DEFAULT '',
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER NOT NULL,
                predicate TEXT NOT NULL,
                object_id INTEGER NOT NULL,
                weight REAL DEFAULT 1.0,
                source TEXT DEFAULT '',
                created_at REAL,
                FOREIGN KEY (subject_id) REFERENCES entities(id),
                FOREIGN KEY (object_id) REFERENCES entities(id)
            );
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
            CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
            CREATE INDEX IF NOT EXISTS idx_relations_subj ON relations(subject_id);
            CREATE INDEX IF NOT EXISTS idx_relations_obj ON relations(object_id);
            CREATE INDEX IF NOT EXISTS idx_relations_predicate ON relations(predicate);
        """)
        self._conn.commit()

    def add_entity(self, name: str, etype: str = "unknown", source: str = "") -> int:
        """Add or update an entity with validation. Returns entity id."""
        # Validate entity name
        if not name or not isinstance(name, str):
            raise ValueError("Entity name must be a non-empty string")
        
        name = name.strip()
        if not name:
            raise ValueError("Entity name cannot be empty after trimming")
        
        if len(name) > 200:
            raise ValueError("Entity name too long (max 200 chars)")
        
        # Validate characters - allow word chars, whitespace, hyphens, dots
        if not re.match(r'^[\w\s\-\.]+$', name):
            raise ValueError("Entity name contains invalid characters")
        
        # Block HTML tags and script injection
        if re.search(r'<[^>]*>', name):
            raise ValueError("Entity name contains HTML tags")
        
        # Normalize whitespace
        name = re.sub(r'\s+', ' ', name)
        
        now = time.time()
        cur = self._conn.execute("SELECT id FROM entities WHERE name = ?", (name,))
        row = cur.fetchone()
        if row:
            self._conn.execute(
                "UPDATE entities SET type = ?, updated_at = ? WHERE id = ?",
                (etype, now, row["id"])
            )
            return row["id"]
        cur = self._conn.execute(
            "INSERT INTO entities (name, type, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, etype, source, now, now)
        )
        self._conn.commit()
        return cur.lastrowid

    def add_relation(self, subject: str, predicate: str, obj: str,
                    weight: float = 1.0, source: str = "") -> bool:
        """Add a relationship between two entities with transaction support."""
        # Validate predicate
        if not predicate or not isinstance(predicate, str):
            raise ValueError("Predicate must be a non-empty string")
        predicate = predicate.strip()
        if len(predicate) > 200:
            raise ValueError("Predicate too long (max 200 chars)")
        
        # Validate weight
        if not isinstance(weight, (int, float)) or weight < 0:
            raise ValueError("Weight must be a non-negative number")
        
        subj_id = self.add_entity(subject, source=source)
        obj_id = self.add_entity(obj, source=source)

        try:
            with self._conn:  # automatic transaction
                # Check if relation already exists
                cur = self._conn.execute(
                    "SELECT id FROM relations WHERE subject_id = ? AND predicate = ? AND object_id = ?",
                    (subj_id, predicate, obj_id)
                )
                if cur.fetchone():
                    # Update weight
                    self._conn.execute(
                        "UPDATE relations SET weight = weight + ? WHERE subject_id = ? AND predicate = ? AND object_id = ?",
                        (weight, subj_id, predicate, obj_id)
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO relations (subject_id, predicate, object_id, weight, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (subj_id, predicate, obj_id, weight, source, time.time())
                    )
        except sqlite3.Error as e:
            logger.error("Transaction failed in add_relation: %s", e)
            raise
        return True

    def query_entity(self, name: str) -> Optional[Dict[str, Any]]:
        """Get entity info and its relationships."""
        cur = self._conn.execute("SELECT * FROM entities WHERE name = ?", (name,))
        row = cur.fetchone()
        if not row:
            return None

        # Get outgoing relations
        cur = self._conn.execute("""
            SELECT e.name as object_name, e.type as object_type, r.predicate, r.weight
            FROM relations r
            JOIN entities e ON e.id = r.object_id
            WHERE r.subject_id = ?
            ORDER BY r.weight DESC
        """, (row["id"],))
        outgoing = [dict(r) for r in cur.fetchall()]

        # Get incoming relations
        cur = self._conn.execute("""
            SELECT e.name as subject_name, e.type as subject_type, r.predicate, r.weight
            FROM relations r
            JOIN entities e ON e.id = r.subject_id
            WHERE r.object_id = ?
            ORDER BY r.weight DESC
        """, (row["id"],))
        incoming = [dict(r) for r in cur.fetchall()]

        return {
            "name": row["name"],
            "type": row["type"],
            "outgoing": outgoing,
            "incoming": incoming,
        }

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search entities by name (LIKE)."""
        assert query, "query cannot be empty"
        assert isinstance(query, str), "query must be a string"
        assert limit > 0, "limit must be positive"
        
        cur = self._conn.execute(
            "SELECT * FROM entities WHERE name LIKE ? LIMIT ?",
            (f"%{query}%", limit)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_neighbors(self, name: str, depth: int = 1) -> List[Dict[str, Any]]:
        """Get all entities within N hops of the given entity."""
        assert name, "name cannot be empty"
        assert isinstance(name, str), "name must be a string"
        assert depth > 0, "depth must be positive"
        
        entity = self.query_entity(name)
        if not entity:
            return []

        visited = {name}
        result = [entity]
        current = [name]

        for _ in range(depth):
            next_level = []
            for node_name in current:
                node = self.query_entity(node_name)
                if not node:
                    continue
                for rel in node.get("outgoing", []):
                    neighbor = rel["object_name"]
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_level.append(neighbor)
                        result.append({
                            "name": neighbor,
                            "relation": f"{node_name} --[{rel['predicate']}]--> {neighbor}",
                        })
            current = next_level
            if not current:
                break

        return result

    def extract_from_text(self, text: str, source: str = "") -> int:
        """Simple rule-based entity extraction from text."""
        assert text, "text cannot be empty"
        assert isinstance(text, str), "text must be a string"
        
        count = 0

        # Extract proper nouns (capitalized words, Chinese names)
        # English: capitalized sequences
        for match in re.finditer(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text):
            name = match.group()
            if len(name) > 3:
                self.add_entity(name, etype="unknown", source=source)
                count += 1

        # Chinese: 2-4 character sequences that look like names/terms
        for match in re.finditer(r'[\u4e00-\u9fff]{2,4}', text):
            name = match.group()
            # Skip common words
            if name not in ("一个", "这个", "那个", "我们", "他们", "什么", "可以", "就是", "没有",
                          "因为", "所以", "但是", "如果", "已经", "还是", "不过", "虽然"):
                self.add_entity(name, etype="unknown", source=source)
                count += 1

        return count

    def stats(self) -> Dict[str, Any]:
        """Get graph statistics."""
        entity_count = self._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        relation_count = self._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        return {
            "entities": entity_count,
            "relations": relation_count,
        }

    # -------------------------------------------------------- async wrappers

    async def add_entity_async(self, name: str, etype: str = "unknown", source: str = "") -> int:
        """Async wrapper for add_entity"""
        return await asyncio.to_thread(self.add_entity, name, etype, source)

    async def add_relation_async(
        self, subject: str, predicate: str, obj: str,
        weight: float = 1.0, source: str = ""
    ) -> bool:
        """Async wrapper for add_relation"""
        return await asyncio.to_thread(self.add_relation, subject, predicate, obj, weight, source)

    async def query_entity_async(self, name: str) -> Optional[Dict[str, Any]]:
        """Async wrapper for query_entity"""
        return await asyncio.to_thread(self.query_entity, name)

    async def search_async(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Async wrapper for search"""
        return await asyncio.to_thread(self.search, query, limit)

    async def get_neighbors_async(self, name: str, depth: int = 1) -> List[Dict[str, Any]]:
        """Async wrapper for get_neighbors"""
        return await asyncio.to_thread(self.get_neighbors, name, depth)

    async def extract_from_text_async(self, text: str, source: str = "") -> int:
        """Async wrapper for extract_from_text"""
        return await asyncio.to_thread(self.extract_from_text, text, source)

    async def stats_async(self) -> Dict[str, Any]:
        """Async wrapper for stats"""
        return await asyncio.to_thread(self.stats)


# ------------------------------------------------------------------ skill handler factory

def make_graph_search_handler(kg):
    """Create a handler for graph_search skill that queries the knowledge graph.

    The handler expects ``kg`` to be a ``KnowledgeGraph`` instance (or any
    object with ``search``, ``query_entity``, and ``get_neighbors`` methods).
    """
    async def handler(args):
        action = args.get("action", "search")
        if action == "search":
            query = args.get("query", args.get("input", ""))
            if not query:
                return "请提供搜索关键词"
            results = kg.search(query, limit=args.get("limit", 10))
            if not results:
                return f"未找到与 '{query}' 相关的实体"
            lines = [f"实体搜索结果（{query}）："]
            for r in results:
                lines.append(f"  - {r['name']} (类型: {r.get('type', 'unknown')})")
            return "\n".join(lines)
        elif action == "entity":
            name = args.get("name", args.get("input", ""))
            if not name:
                return "请提供实体名称"
            entity = kg.query_entity(name)
            if entity is None:
                return f"未找到实体: {name}"
            lines = [f"实体: {entity['name']} (类型: {entity['type']})"]
            if entity["outgoing"]:
                lines.append("  出边关系:")
                for r in entity["outgoing"]:
                    lines.append(f"    --[{r['predicate']}]--> {r['object_name']}")
            if entity["incoming"]:
                lines.append("  入边关系:")
                for r in entity["incoming"]:
                    lines.append(f"    {r['subject_name']} --[{r['predicate']}]-->")
            return "\n".join(lines)
        elif action == "neighbors":
            name = args.get("name", args.get("input", ""))
            if not name:
                return "请提供实体名称"
            depth = args.get("depth", 1)
            neighbors = kg.get_neighbors(name, depth=depth)
            if not neighbors:
                return f"未找到与 '{name}' 相关的邻居"
            lines = [f"邻居图谱（源于 {name}，深度 {depth}）："]
            for n in neighbors:
                if "relation" in n:
                    lines.append(f"  {n['relation']}")
                else:
                    lines.append(f"  中心实体: {n['name']}")
            return "\n".join(lines)
        else:
            return f"未知操作: {action}，支持: search, entity, neighbors"
    return handler