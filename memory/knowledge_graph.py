"""Knowledge graph memory — entity extraction and relationship queries."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from .base_store import BaseSQLiteStore

logger = logging.getLogger(__name__)

STOPWORDS = {"一个", "这个", "那个", "我们", "他们", "什么", "可以", "就是", "没有",
             "因为", "所以", "但是", "如果", "已经", "还是", "不过", "虽然"}


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
        with self._write_lock:
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

    def add_entities_batch(self, names: List[str], etype: str = "unknown", source: str = "") -> int:
        """Add or update multiple entities in a single transaction. Returns count added."""
        if not names:
            return 0
        # Deduplicate while preserving order
        seen = set()
        unique_names = []
        for name in names:
            name = name.strip()
            if not name or len(name) > 200:
                continue
            # Normalize whitespace
            name = re.sub(r'\s+', ' ', name)
            if name in seen:
                continue
            seen.add(name)
            unique_names.append(name)

        if not unique_names:
            return 0

        now = time.time()
        added = 0
        with self._write_lock:
            for name in unique_names:
                # Validate
                if not re.match(r'^[\w\s\-\.]+$', name):
                    continue
                if re.search(r'<[^>]*>', name):
                    continue
                # Upsert
                cur = self._conn.execute("SELECT id FROM entities WHERE name = ?", (name,))
                row = cur.fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE entities SET type = ?, updated_at = ? WHERE id = ?",
                        (etype, now, row["id"])
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO entities (name, type, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (name, etype, source, now, now)
                    )
                    added += 1
            self._conn.commit()
        return added

    def add_relation(self, subject: str, predicate: str, obj: str,
                    weight: float = 1.0, source: str = "") -> bool:
        """Add a relationship between two entities with transaction support.

        P0-4 fix: entity upsert and relation insert are now in the same
        transaction. Previously, add_entity() was called separately (each
        committing independently), so a failure during relation insert
        would leave orphaned entities in the database.
        """
        # Validate predicate
        if not predicate or not isinstance(predicate, str):
            raise ValueError("Predicate must be a non-empty string")
        predicate = predicate.strip()
        if len(predicate) > 200:
            raise ValueError("Predicate too long (max 200 chars)")

        # Validate weight
        if not isinstance(weight, (int, float)) or weight < 0:
            raise ValueError("Weight must be a non-negative number")

        now = time.time()
        with self._write_lock:
            try:
                with self._conn:  # single atomic transaction
                    # Upsert subject entity (inline — don't call add_entity
                    # which commits independently)
                    subj_id = self._upsert_entity_tx(subject, now, source)
                    obj_id = self._upsert_entity_tx(obj, now, source)

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

    def _upsert_entity_tx(self, name: str, now: float, source: str = "") -> int:
        """Upsert an entity within the current transaction (no commit).

        This is the transaction-safe version of add_entity used by
        add_relation to avoid independent commits that break atomicity.
        """
        name = name.strip()
        name = re.sub(r'\s+', ' ', name)
        cur = self._conn.execute("SELECT id FROM entities WHERE name = ?", (name,))
        row = cur.fetchone()
        if row:
            self._conn.execute(
                "UPDATE entities SET type = ?, updated_at = ? WHERE id = ?",
                ("unknown", now, row["id"])
            )
            return row["id"]
        cur = self._conn.execute(
            "INSERT INTO entities (name, type, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, "unknown", source, now, now)
        )
        return cur.lastrowid

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

    def query_entities_batch(self, names: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """Batch-fetch entity info + relations for multiple entities. Returns {name: entity_dict or None}."""
        if not names:
            return {}
        placeholders = ",".join("?" * len(names))
        cur = self._conn.execute(f"SELECT * FROM entities WHERE name IN ({placeholders})", names)
        entities = {row["name"]: dict(row) for row in cur.fetchall()}

        # Batch-fetch all outgoing + incoming relations
        all_ids = [e["id"] for e in entities.values()]
        if not all_ids:
            return {}

        id_placeholders = ",".join("?" * len(all_ids))

        out_cur = self._conn.execute(f"""
            SELECT r.*, e.name as object_name, e.type as object_type
            FROM relations r
            JOIN entities e ON e.id = r.object_id
            WHERE r.subject_id IN ({id_placeholders})
            ORDER BY r.weight DESC
        """, all_ids)

        in_cur = self._conn.execute(f"""
            SELECT r.*, e.name as subject_name, e.type as subject_type
            FROM relations r
            JOIN entities e ON e.id = r.subject_id
            WHERE r.object_id IN ({id_placeholders})
            ORDER BY r.weight DESC
        """, all_ids)

        # Build outgoing/incoming maps
        outgoing: Dict[int, List] = {i: [] for i in all_ids}
        incoming: Dict[int, List] = {i: [] for i in all_ids}
        for row in out_cur.fetchall():
            outgoing[row["subject_id"]].append({"object_name": row["object_name"], "object_type": row["object_type"], "predicate": row["predicate"], "weight": row["weight"]})
        for row in in_cur.fetchall():
            incoming[row["object_id"]].append({"subject_name": row["subject_name"], "subject_type": row["subject_type"], "predicate": row["predicate"], "weight": row["weight"]})

        result = {}
        for name, entity in entities.items():
            result[name] = {
                "name": entity["name"],
                "type": entity["type"],
                "outgoing": outgoing.get(entity["id"], []),
                "incoming": incoming.get(entity["id"], []),
            }

        return result

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search entities by name (LIKE)."""
        if not query:
            raise ValueError("query cannot be empty")
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if limit <= 0:
            raise ValueError("limit must be positive")

        query = query[:200]

        escaped_query = query.replace('%', '\\%').replace('_', '\\_')

        cur = self._conn.execute(
            "SELECT * FROM entities WHERE name LIKE ? ESCAPE '\\' LIMIT ?",
            (f"%{escaped_query}%", limit)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_neighbors(self, name: str, depth: int = 1) -> List[Dict[str, Any]]:
        """Get all entities within N hops of the given entity."""
        if not name:
            raise ValueError("name cannot be empty")
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        if depth <= 0:
            raise ValueError("depth must be positive")

        entity = self.query_entity(name)
        if not entity:
            return []

        visited = {name}
        result = [entity]
        current = [name]

        for _ in range(depth):
            next_level = []
            # Batch-fetch all current-level nodes at once
            batch_nodes = self.query_entities_batch(current)
            for node_name in current:
                node = batch_nodes.get(node_name)
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
        if not text:
            raise ValueError("text cannot be empty")
        if not isinstance(text, str):
            raise ValueError("text must be a string")

        names: List[str] = []

        # Extract proper nouns (capitalized words, Chinese names)
        # English: capitalized sequences
        for match in re.finditer(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text):
            name = match.group()
            if len(name) > 3:
                names.append(name)

        # Chinese: 2-4 character sequences that look like names/terms
        for match in re.finditer(r'[\u4e00-\u9fff]{2,4}', text):
            name = match.group()
            if name not in STOPWORDS:
                names.append(name)

        return self.add_entities_batch(names, etype="unknown", source=source)

    # ===================================================== Graph Reasoning

    def find_path(self, start: str, end: str, max_depth: int = 3) -> Optional[List[Dict[str, Any]]]:
        """Find a path between two entities using BFS.

        Returns a list of relations forming the path from start to end,
        or None if no path exists within max_depth.

        Example path:
        [
            {"from": "Python", "predicate": "is_a", "to": "编程语言"},
            {"from": "编程语言", "predicate": "用于", "to": "软件开发"},
        ]
        """
        if start == end:
            return []

        # BFS with path tracking
        visited = {start}
        # Queue items: (current_entity, path_so_far)
        queue: List[Tuple[str, List[Dict[str, Any]]]] = [(start, [])]

        for _ in range(max_depth):
            next_queue = []
            # Collect all entities at this level for batch query
            level_entities = [item[0] for item in queue if item[0] not in {""}]
            if not level_entities:
                break

            batch = self.query_entities_batch(level_entities)

            for entity_name, path in queue:
                entity = batch.get(entity_name)
                if not entity:
                    continue

                for rel in entity.get("outgoing", []):
                    neighbor = rel["object_name"]
                    if neighbor in visited:
                        continue

                    new_path = path + [{
                        "from": entity_name,
                        "predicate": rel["predicate"],
                        "to": neighbor,
                        "weight": rel["weight"],
                    }]

                    if neighbor == end:
                        return new_path

                    visited.add(neighbor)
                    next_queue.append((neighbor, new_path))

            queue = next_queue
            if not queue:
                break

        return None

    def find_common_neighbors(self, entity_a: str, entity_b: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Find entities connected to both A and B.

        Returns common neighbors with their connections to both entities,
        sorted by combined weight.
        """
        entity_a_data = self.query_entity(entity_a)
        entity_b_data = self.query_entity(entity_b)

        if not entity_a_data or not entity_b_data:
            return []

        # Collect all neighbors of A with their relations
        a_neighbors: Dict[str, List[Dict[str, Any]]] = {}
        for rel in entity_a_data.get("outgoing", []):
            name = rel["object_name"]
            a_neighbors.setdefault(name, []).append({
                "direction": "out",
                "predicate": rel["predicate"],
                "weight": rel["weight"],
            })
        for rel in entity_a_data.get("incoming", []):
            name = rel["subject_name"]
            a_neighbors.setdefault(name, []).append({
                "direction": "in",
                "predicate": rel["predicate"],
                "weight": rel["weight"],
            })

        # Collect all neighbors of B
        b_neighbors: Dict[str, List[Dict[str, Any]]] = {}
        for rel in entity_b_data.get("outgoing", []):
            name = rel["object_name"]
            b_neighbors.setdefault(name, []).append({
                "direction": "out",
                "predicate": rel["predicate"],
                "weight": rel["weight"],
            })
        for rel in entity_b_data.get("incoming", []):
            name = rel["subject_name"]
            b_neighbors.setdefault(name, []).append({
                "direction": "in",
                "predicate": rel["predicate"],
                "weight": rel["weight"],
            })

        # Find intersection
        common_names = set(a_neighbors.keys()) & set(b_neighbors.keys())

        result = []
        for name in common_names:
            a_rels = a_neighbors[name]
            b_rels = b_neighbors[name]
            total_weight = sum(r["weight"] for r in a_rels) + sum(r["weight"] for r in b_rels)
            result.append({
                "name": name,
                "relations_from_a": a_rels,
                "relations_from_b": b_rels,
                "combined_weight": total_weight,
            })

        # Sort by combined weight
        result.sort(key=lambda x: x["combined_weight"], reverse=True)
        return result[:limit]

    def infer_relations(self, entity: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Infer likely relations for an entity using transitive reasoning.

        Uses the pattern: A --[rel1]--> B --[rel2]--> C
        to hypothesize: A --[likely]--> C

        Returns inferred connections ranked by confidence.
        """
        entity_data = self.query_entity(entity)
        if not entity_data:
            return []

        # Get 1-hop neighbors
        direct_neighbors = {rel["object_name"] for rel in entity_data.get("outgoing", [])}

        # Collect 2-hop connections
        inferred: Dict[str, Dict[str, Any]] = {}

        for rel_1 in entity_data.get("outgoing", []):
            neighbor = rel_1["object_name"]
            neighbor_data = self.query_entity(neighbor)
            if not neighbor_data:
                continue

            for rel_2 in neighbor_data.get("outgoing", []):
                target = rel_2["object_name"]
                # Skip direct connections (we already know those)
                if target in direct_neighbors or target == entity:
                    continue

                key = f"{entity}->{target}"
                if key not in inferred:
                    inferred[key] = {
                        "target": target,
                        "confidence": 0.0,
                        "paths": [],
                    }

                # Confidence = product of weights along the path
                path_confidence = rel_1["weight"] * rel_2["weight"] * 0.5  # decay
                inferred[key]["confidence"] += path_confidence
                inferred[key]["paths"].append({
                    "via": neighbor,
                    "predicate_1": rel_1["predicate"],
                    "predicate_2": rel_2["predicate"],
                    "path_weight": path_confidence,
                })

        # Sort by confidence
        result = sorted(inferred.values(), key=lambda x: x["confidence"], reverse=True)
        return result[:top_k]

    def get_entity_clusters(self, min_size: int = 2) -> List[Dict[str, Any]]:
        """Find densely connected entity clusters (community detection).

        Uses a simple shared-neighbor clustering approach.
        Returns clusters sorted by size.
        """
        # Get all entity names (sample for performance on large graphs)
        cur = self._conn.execute("SELECT name FROM entities ORDER BY id DESC LIMIT 500")
        all_entities = [row["name"] for row in cur.fetchall()]

        if len(all_entities) < min_size:
            return []

        # Build adjacency sets
        adjacency: Dict[str, set] = {}
        batch = self.query_entities_batch(all_entities)

        for name, entity in batch.items():
            if not entity:
                continue
            neighbors = set()
            for rel in entity.get("outgoing", []):
                neighbors.add(rel["object_name"])
            for rel in entity.get("incoming", []):
                neighbors.add(rel["subject_name"])
            adjacency[name] = neighbors

        # Simple clustering: greedy group by shared neighbors
        visited = set()
        clusters: List[Dict[str, Any]] = []

        for entity in all_entities:
            if entity in visited:
                continue

            # Start a new cluster
            cluster = {entity}
            visited.add(entity)

            # Grow cluster by adding entities with many shared neighbors
            changed = True
            while changed:
                changed = False
                best_candidate = None
                best_score = 0

                for candidate in all_entities:
                    if candidate in visited:
                        continue

                    # Count shared neighbors with cluster
                    candidate_neighbors = adjacency.get(candidate, set())
                    shared = 0
                    for member in cluster:
                        shared += len(candidate_neighbors & adjacency.get(member, set()))

                    if shared > best_score and shared >= min_size:
                        best_score = shared
                        best_candidate = candidate

                if best_candidate:
                    cluster.add(best_candidate)
                    visited.add(best_candidate)
                    changed = True

            if len(cluster) >= min_size:
                clusters.append({
                    "entities": sorted(cluster),
                    "size": len(cluster),
                })

        # Sort by size descending
        clusters.sort(key=lambda x: x["size"], reverse=True)
        return clusters

    def stats(self) -> Dict[str, Any]:
        """Get graph statistics."""
        try:
            entity_count = self._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            relation_count = self._conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

            avg_degree = (relation_count * 2 / entity_count) if entity_count > 0 else 0

            return {
                "entities": entity_count,
                "relations": relation_count,
                "avg_degree": round(avg_degree, 2),
            }
        except Exception:
            return {}


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
