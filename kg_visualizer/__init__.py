"""知识图谱可视化 — 知识图谱交互式展示、实体关系管理。

提供：
  - 知识图谱数据结构
  - 实体/关系增删改查
  - 图谱查询（路径查找、关联分析）
  - 图谱导入导出
  - 可视化数据格式转换
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class Entity:
    """实体类。"""
    entity_id: str
    name: str
    entity_type: str = "default"
    description: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class Relation:
    """关系类。"""
    relation_id: str
    source_id: str
    target_id: str
    relation_type: str
    description: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    weight: float = 1.0


@dataclass
class GraphQueryResult:
    """图谱查询结果。"""
    entities: List[Entity] = field(default_factory=list)
    relations: List[Relation] = field(default_factory=list)
    paths: List[List[str]] = field(default_factory=list)


class KnowledgeGraph:
    """知识图谱类 — 管理实体和关系。"""

    def __init__(self, graph_id: str = "default", name: str = "默认图谱"):
        self.graph_id = graph_id
        self.name = name
        self._entities: Dict[str, Entity] = {}
        self._relations: List[Relation] = []
        self._adjacency: Dict[str, Dict[str, List[Relation]]] = {}  # source -> target -> [relations]

    def add_entity(self, name: str, entity_type: str = "default",
                   description: str = "", properties: Dict = None) -> Entity:
        """添加实体。"""
        entity_id = f"ent_{uuid.uuid4().hex[:12]}"
        entity = Entity(
            entity_id=entity_id,
            name=name,
            entity_type=entity_type,
            description=description,
            properties=properties or {},
        )
        self._entities[entity_id] = entity
        self._adjacency[entity_id] = {}
        return entity

    def update_entity(self, entity_id: str, **kwargs) -> Optional[Entity]:
        """更新实体。"""
        entity = self._entities.get(entity_id)
        if not entity:
            return None

        for key, value in kwargs.items():
            if hasattr(entity, key):
                setattr(entity, key, value)

        entity.updated_at = time.time()
        return entity

    def delete_entity(self, entity_id: str) -> bool:
        """删除实体（级联删除相关关系）。"""
        if entity_id not in self._entities:
            return False

        # 删除相关关系
        self._relations = [
            r for r in self._relations
            if r.source_id != entity_id and r.target_id != entity_id
        ]

        # 删除邻接表
        if entity_id in self._adjacency:
            del self._adjacency[entity_id]

        for source_id, targets in self._adjacency.items():
            if entity_id in targets:
                del targets[entity_id]

        del self._entities[entity_id]
        return True

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """获取实体。"""
        return self._entities.get(entity_id)

    def find_entity_by_name(self, name: str) -> List[Entity]:
        """按名称查找实体。"""
        return [e for e in self._entities.values() if name.lower() in e.name.lower()]

    def list_entities(self, entity_type: str = None, limit: int = 100) -> List[Entity]:
        """列出实体。"""
        results = list(self._entities.values())
        if entity_type:
            results = [e for e in results if e.entity_type == entity_type]
        return results[:limit]

    def add_relation(self, source_id: str, target_id: str, relation_type: str,
                     description: str = "", properties: Dict = None,
                     weight: float = 1.0) -> Optional[Relation]:
        """添加关系。"""
        if source_id not in self._entities or target_id not in self._entities:
            return None

        relation_id = f"rel_{uuid.uuid4().hex[:12]}"
        relation = Relation(
            relation_id=relation_id,
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            description=description,
            properties=properties or {},
            weight=weight,
        )

        self._relations.append(relation)

        if source_id not in self._adjacency:
            self._adjacency[source_id] = {}
        if target_id not in self._adjacency[source_id]:
            self._adjacency[source_id][target_id] = []
        self._adjacency[source_id][target_id].append(relation)

        return relation

    def delete_relation(self, relation_id: str) -> bool:
        """删除关系。"""
        for i, rel in enumerate(self._relations):
            if rel.relation_id == relation_id:
                # 从邻接表移除
                if rel.source_id in self._adjacency:
                    if rel.target_id in self._adjacency[rel.source_id]:
                        self._adjacency[rel.source_id][rel.target_id] = [
                            r for r in self._adjacency[rel.source_id][rel.target_id]
                            if r.relation_id != relation_id
                        ]
                self._relations.pop(i)
                return True
        return False

    def get_relations(self, entity_id: str, direction: str = "both") -> List[Relation]:
        """获取实体的关系。"""
        results = []
        for rel in self._relations:
            if direction == "out" and rel.source_id == entity_id:
                results.append(rel)
            elif direction == "in" and rel.target_id == entity_id:
                results.append(rel)
            elif direction == "both" and (rel.source_id == entity_id or rel.target_id == entity_id):
                results.append(rel)
        return results

    def find_path(self, start_id: str, end_id: str, max_depth: int = 3) -> List[List[str]]:
        """查找两个实体之间的路径（BFS）。"""
        if start_id not in self._entities or end_id not in self._entities:
            return []

        if start_id == end_id:
            return [[start_id]]

        visited = set()
        queue = [(start_id, [start_id])]
        paths = []

        while queue:
            current, path = queue.pop(0)

            if len(path) > max_depth + 1:
                continue

            if current == end_id:
                paths.append(path)
                continue

            if current in visited:
                continue
            visited.add(current)

            if current in self._adjacency:
                for neighbor in self._adjacency[current]:
                    if neighbor not in visited:
                        queue.append((neighbor, path + [neighbor]))

        return paths

    def get_neighbors(self, entity_id: str, depth: int = 1) -> GraphQueryResult:
        """获取实体的邻居（指定深度）。"""
        result = GraphQueryResult()
        if entity_id not in self._entities:
            return result

        visited = {entity_id}
        current_level = {entity_id}

        for _ in range(depth):
            next_level = set()
            for eid in current_level:
                if eid in self._adjacency:
                    for target_id, rels in self._adjacency[eid].items():
                        result.relations.extend(rels)
                        if target_id not in visited:
                            visited.add(target_id)
                            next_level.add(target_id)
            current_level = next_level

        result.entities = [self._entities[eid] for eid in visited if eid in self._entities]
        return result

    def search_graph(self, query: str, limit: int = 50) -> GraphQueryResult:
        """搜索图谱（按名称匹配实体和关系）。"""
        result = GraphQueryResult()
        query_lower = query.lower()

        # 匹配实体
        matched_entities = []
        for entity in self._entities.values():
            if (query_lower in entity.name.lower() or
                query_lower in entity.description.lower() or
                any(query_lower in str(v).lower() for v in entity.properties.values())):
                matched_entities.append(entity)

        # 获取匹配实体的直接邻居
        entity_ids = set()
        for entity in matched_entities[:limit]:
            entity_ids.add(entity.entity_id)
            neighbors = self.get_neighbors(entity.entity_id, depth=1)
            for e in neighbors.entities:
                entity_ids.add(e.entity_id)
            result.relations.extend(neighbors.relations)

        result.entities = [self._entities[eid] for eid in entity_ids if eid in self._entities]
        return result

    def get_entity_types(self) -> Dict[str, int]:
        """获取实体类型统计。"""
        types = {}
        for entity in self._entities.values():
            types[entity.entity_type] = types.get(entity.entity_type, 0) + 1
        return types

    def get_relation_types(self) -> Dict[str, int]:
        """获取关系类型统计。"""
        types = {}
        for rel in self._relations:
            types[rel.relation_type] = types.get(rel.relation_type, 0) + 1
        return types

    def export_json(self) -> Dict[str, Any]:
        """导出图谱为JSON。"""
        return {
            "graph_id": self.graph_id,
            "name": self.name,
            "entities": [e.__dict__ for e in self._entities.values()],
            "relations": [r.__dict__ for r in self._relations],
            "stats": {
                "entity_count": len(self._entities),
                "relation_count": len(self._relations),
                "entity_types": self.get_entity_types(),
                "relation_types": self.get_relation_types(),
            },
        }

    def import_json(self, data: Dict[str, Any]) -> bool:
        """从JSON导入图谱。"""
        try:
            self.graph_id = data.get("graph_id", self.graph_id)
            self.name = data.get("name", self.name)

            # 导入实体
            for e_data in data.get("entities", []):
                entity = Entity(**e_data)
                self._entities[entity.entity_id] = entity
                self._adjacency[entity.entity_id] = {}

            # 导入关系
            for r_data in data.get("relations", []):
                relation = Relation(**r_data)
                self._relations.append(relation)

                if relation.source_id not in self._adjacency:
                    self._adjacency[relation.source_id] = {}
                if relation.target_id not in self._adjacency[relation.source_id]:
                    self._adjacency[relation.source_id][relation.target_id] = []
                self._adjacency[relation.source_id][relation.target_id].append(relation)

            return True
        except Exception as exc:
            logger.error("Failed to import graph: %s", exc)
            return False

    def to_vis_data(self) -> Dict[str, Any]:
        """转换为可视化数据格式（vis.js兼容）。"""
        nodes = []
        edges = []

        for entity in self._entities.values():
            nodes.append({
                "id": entity.entity_id,
                "label": entity.name,
                "title": entity.description,
                "group": entity.entity_type,
            })

        for rel in self._relations:
            edges.append({
                "id": rel.relation_id,
                "from": rel.source_id,
                "to": rel.target_id,
                "label": rel.relation_type,
                "title": rel.description,
                "value": rel.weight,
            })

        return {"nodes": nodes, "edges": edges}

    def stats(self) -> Dict[str, Any]:
        """获取图谱统计信息。"""
        return {
            "entity_count": len(self._entities),
            "relation_count": len(self._relations),
            "entity_types": self.get_entity_types(),
            "relation_types": self.get_relation_types(),
        }


class KnowledgeGraphVisualizerPlugin(Plugin):
    """知识图谱可视化插件。"""

    name = "kg_visualizer"

    def __init__(self):
        super().__init__()
        self._graphs: Dict[str, KnowledgeGraph] = {}
        self._default_graph = KnowledgeGraph()
        self._graphs["default"] = self._default_graph

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        logger.info("Knowledge graph visualizer plugin configured")

    def create_graph(self, graph_id: str, name: str) -> KnowledgeGraph:
        """创建新图谱。"""
        graph = KnowledgeGraph(graph_id=graph_id, name=name)
        self._graphs[graph_id] = graph
        return graph

    def get_graph(self, graph_id: str = "default") -> Optional[KnowledgeGraph]:
        """获取图谱。"""
        return self._graphs.get(graph_id)

    def list_graphs(self) -> List[Dict[str, Any]]:
        """列出所有图谱。"""
        return [
            {
                "graph_id": gid,
                "name": g.name,
                "stats": g.stats(),
            }
            for gid, g in self._graphs.items()
        ]

    def delete_graph(self, graph_id: str) -> bool:
        """删除图谱。"""
        if graph_id == "default":
            return False
        if graph_id in self._graphs:
            del self._graphs[graph_id]
            return True
        return False

    def export_graph(self, graph_id: str, file_path: str) -> bool:
        """导出图谱到文件。"""
        graph = self._graphs.get(graph_id)
        if not graph:
            return False

        try:
            data = graph.export_json()
            Path(file_path).parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as exc:
            logger.warning("Failed to export graph: %s", exc)
            return False

    def import_graph(self, graph_id: str, file_path: str) -> bool:
        """从文件导入图谱。"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            graph = KnowledgeGraph(graph_id=graph_id, name=data.get("name", graph_id))
            graph.import_json(data)
            self._graphs[graph_id] = graph
            return True
        except Exception as exc:
            logger.warning("Failed to import graph: %s", exc)
            return False

    def get_visualization_data(self, graph_id: str = "default") -> Optional[Dict[str, Any]]:
        """获取可视化数据。"""
        graph = self._graphs.get(graph_id)
        if graph:
            return graph.to_vis_data()
        return None