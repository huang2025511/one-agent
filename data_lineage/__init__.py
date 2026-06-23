"""数据血缘与可观测性增强模块。

提供端到端的数据可观测性能力：
  - 数据流转追踪（DataFlowTracker）：追踪数据从源到目标的完整流转路径
  - 字段级血缘（FieldLineage）：映射字段级别的血缘关系，追踪字段来源与派生
  - 影响分析（ImpactAnalyzer）：评估数据变更的影响范围，上游/下游影响分析
  - 合规审计（ComplianceAudit）：数据访问审计追踪，合规性检查（GDPR/数据保留策略）
  - 数据质量监控（DataQualityMonitor）：质量规则定义、质量评分、异常检测与告警
  - 血缘可视化（LineageVisualizer）：生成 DAG 图数据，支持导出 JSON/Graphviz 格式
  - DataLineagePlugin：整合以上功能的插件类

纯 Python 实现，仅依赖标准库。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# ============================================================
# 数据结构定义
# ============================================================

@dataclass
class DataNode:
    """数据节点 — 表示数据源或数据目标（表、文件、API、流等）。"""
    node_id: str
    name: str
    node_type: str = "dataset"  # dataset / table / file / api / stream / topic
    description: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class ProcessingNode:
    """处理节点 — 表示对数据执行的处理操作（ETL、转换、聚合等）。"""
    node_id: str
    name: str
    operation: str = "transform"  # transform / filter / join / aggregate / enrich / copy
    description: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class DataFlow:
    """数据流转 — 描述一次数据从源经处理节点到目标的流转。"""
    flow_id: str
    source_id: str
    target_id: str
    processing_nodes: List[str] = field(default_factory=list)  # 处理节点 ID 序列
    timestamp: float = field(default_factory=time.time)
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FieldNode:
    """字段节点 — 表示数据集中的字段。"""
    field_id: str
    dataset_id: str  # 所属数据集节点 ID
    field_name: str
    data_type: str = "unknown"
    description: str = ""
    is_pii: bool = False  # 是否为个人敏感信息（PII）


@dataclass
class FieldLineageEdge:
    """字段血缘边 — 描述字段间的派生关系。"""
    edge_id: str
    source_field_id: str
    target_field_id: str
    transformation: str = "direct"  # direct / derived / computed / aggregated / masked
    expression: str = ""  # 转换表达式（如 "a + b"）
    created_at: float = field(default_factory=time.time)


@dataclass
class ImpactReport:
    """影响分析报告。"""
    changed_node_id: str
    upstream: List[str] = field(default_factory=list)
    downstream: List[str] = field(default_factory=list)
    affected_datasets: List[str] = field(default_factory=list)
    affected_fields: List[str] = field(default_factory=list)
    severity: str = "low"  # low / medium / high / critical
    summary: str = ""
    analyzed_at: float = field(default_factory=time.time)


@dataclass
class AuditEntry:
    """审计条目 — 记录一次数据访问。"""
    entry_id: str
    actor: str  # 访问者（用户/服务）
    action: str  # read / write / delete / export / grant
    resource: str  # 被访问的资源标识
    timestamp: float = field(default_factory=time.time)
    success: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetentionPolicy:
    """数据保留策略。"""
    policy_id: str
    name: str
    dataset_pattern: str  # 数据集匹配模式（支持通配符）
    retention_days: int = 365
    action_after: str = "archive"  # archive / delete / anonymize
    enabled: bool = True


@dataclass
class QualityRule:
    """数据质量规则。"""
    rule_id: str
    name: str
    dataset_id: str
    field_name: str = ""
    rule_type: str = "completeness"  # completeness / uniqueness / validity / consistency / timeliness
    expression: str = ""  # 规则表达式或校验函数描述
    threshold: float = 0.95  # 通过阈值（0.0 ~ 1.0）
    severity: str = "warning"  # info / warning / critical
    enabled: bool = True


@dataclass
class QualityReport:
    """数据质量报告。"""
    report_id: str
    dataset_id: str
    rule_id: str
    score: float  # 0.0 ~ 1.0
    passed: bool
    actual_value: float = 0.0
    expected_value: float = 0.0
    message: str = ""
    timestamp: float = field(default_factory=time.time)


# ============================================================
# 数据流转追踪
# ============================================================

class DataFlowTracker:
    """数据流转追踪器 — 追踪数据从源到目标的完整流转路径，记录每个处理节点。"""

    def __init__(self) -> None:
        self._data_nodes: Dict[str, DataNode] = {}
        self._processing_nodes: Dict[str, ProcessingNode] = {}
        self._flows: Dict[str, DataFlow] = {}
        # 邻接表：source_id -> {target_id}，用于上下游分析
        self._downstream: Dict[str, Set[str]] = {}
        self._upstream: Dict[str, Set[str]] = {}

    def register_data_node(
        self,
        name: str,
        node_type: str = "dataset",
        description: str = "",
        properties: Optional[Dict[str, Any]] = None,
    ) -> DataNode:
        """注册一个数据节点。"""
        node_id = f"dn_{uuid.uuid4().hex[:12]}"
        node = DataNode(
            node_id=node_id,
            name=name,
            node_type=node_type,
            description=description,
            properties=properties or {},
        )
        self._data_nodes[node_id] = node
        logger.info("注册数据节点: %s (%s)", name, node_id)
        return node

    def register_processing_node(
        self,
        name: str,
        operation: str = "transform",
        description: str = "",
        properties: Optional[Dict[str, Any]] = None,
    ) -> ProcessingNode:
        """注册一个处理节点。"""
        node_id = f"pn_{uuid.uuid4().hex[:12]}"
        node = ProcessingNode(
            node_id=node_id,
            name=name,
            operation=operation,
            description=description,
            properties=properties or {},
        )
        self._processing_nodes[node_id] = node
        logger.info("注册处理节点: %s (%s)", name, node_id)
        return node

    def record_flow(
        self,
        source_id: str,
        target_id: str,
        processing_nodes: Optional[List[str]] = None,
        properties: Optional[Dict[str, Any]] = None,
    ) -> Optional[DataFlow]:
        """记录一次数据流转。

        要求源节点与目标节点均已注册，处理节点可选。
        """
        if source_id not in self._data_nodes:
            logger.warning("源节点未注册: %s", source_id)
            return None
        if target_id not in self._data_nodes:
            logger.warning("目标节点未注册: %s", target_id)
            return None

        flow_id = f"flow_{uuid.uuid4().hex[:12]}"
        flow = DataFlow(
            flow_id=flow_id,
            source_id=source_id,
            target_id=target_id,
            processing_nodes=processing_nodes or [],
            properties=properties or {},
        )
        self._flows[flow_id] = flow

        # 维护邻接表
        self._downstream.setdefault(source_id, set()).add(target_id)
        self._upstream.setdefault(target_id, set()).add(source_id)
        logger.info("记录数据流转: %s -> %s (flow=%s)", source_id, target_id, flow_id)
        return flow

    def get_flow(self, flow_id: str) -> Optional[DataFlow]:
        """获取指定流转记录。"""
        return self._flows.get(flow_id)

    def list_flows(self) -> List[DataFlow]:
        """列出所有流转记录。"""
        return list(self._flows.values())

    def get_data_node(self, node_id: str) -> Optional[DataNode]:
        """获取数据节点。"""
        return self._data_nodes.get(node_id)

    def get_processing_node(self, node_id: str) -> Optional[ProcessingNode]:
        """获取处理节点。"""
        return self._processing_nodes.get(node_id)

    def list_data_nodes(self) -> List[DataNode]:
        """列出所有数据节点。"""
        return list(self._data_nodes.values())

    def get_downstream(self, node_id: str) -> List[str]:
        """获取直接下游节点。"""
        return list(self._downstream.get(node_id, set()))

    def get_upstream(self, node_id: str) -> List[str]:
        """获取直接上游节点。"""
        return list(self._upstream.get(node_id, set()))

    def get_all_downstream(self, node_id: str) -> List[str]:
        """获取所有下游节点（递归，BFS）。"""
        visited: Set[str] = set()
        queue = [node_id]
        while queue:
            current = queue.pop(0)
            for nxt in self._downstream.get(current, set()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        return list(visited)

    def get_all_upstream(self, node_id: str) -> List[str]:
        """获取所有上游节点（递归，BFS）。"""
        visited: Set[str] = set()
        queue = [node_id]
        while queue:
            current = queue.pop(0)
            for prev in self._upstream.get(current, set()):
                if prev not in visited:
                    visited.add(prev)
                    queue.append(prev)
        return list(visited)

    def trace_path(self, source_id: str, target_id: str, max_depth: int = 10) -> List[List[str]]:
        """追踪从源到目标的所有路径（BFS）。"""
        if source_id not in self._data_nodes or target_id not in self._data_nodes:
            return []
        if source_id == target_id:
            return [[source_id]]

        paths: List[List[str]] = []
        queue: List[Tuple[str, List[str]]] = [(source_id, [source_id])]
        visited_paths: Set[Tuple[str, ...]] = set()

        while queue:
            current, path = queue.pop(0)
            if len(path) > max_depth + 1:
                continue
            if current == target_id:
                paths.append(path)
                continue
            key = tuple(path)
            if key in visited_paths:
                continue
            visited_paths.add(key)
            for nxt in self._downstream.get(current, set()):
                if nxt not in path:  # 避免环路
                    queue.append((nxt, path + [nxt]))
        return paths

    def stats(self) -> Dict[str, Any]:
        """获取追踪器统计信息。"""
        return {
            "data_nodes": len(self._data_nodes),
            "processing_nodes": len(self._processing_nodes),
            "flows": len(self._flows),
        }


# ============================================================
# 字段级血缘
# ============================================================

class FieldLineage:
    """字段级血缘管理器 — 映射字段级别的血缘关系，追踪字段来源与派生关系。"""

    def __init__(self) -> None:
        self._fields: Dict[str, FieldNode] = {}
        self._edges: Dict[str, FieldLineageEdge] = {}
        # 字段级邻接表：source_field_id -> {target_field_id}
        self._field_downstream: Dict[str, Set[str]] = {}
        self._field_upstream: Dict[str, Set[str]] = {}

    def register_field(
        self,
        dataset_id: str,
        field_name: str,
        data_type: str = "unknown",
        description: str = "",
        is_pii: bool = False,
    ) -> FieldNode:
        """注册一个字段。"""
        field_id = f"fld_{uuid.uuid4().hex[:12]}"
        node = FieldNode(
            field_id=field_id,
            dataset_id=dataset_id,
            field_name=field_name,
            data_type=data_type,
            description=description,
            is_pii=is_pii,
        )
        self._fields[field_id] = node
        logger.info("注册字段: %s.%s (%s)", dataset_id, field_name, field_id)
        return node

    def add_lineage(
        self,
        source_field_id: str,
        target_field_id: str,
        transformation: str = "direct",
        expression: str = "",
    ) -> Optional[FieldLineageEdge]:
        """添加字段血缘关系。"""
        if source_field_id not in self._fields:
            logger.warning("源字段未注册: %s", source_field_id)
            return None
        if target_field_id not in self._fields:
            logger.warning("目标字段未注册: %s", target_field_id)
            return None

        edge_id = f"edge_{uuid.uuid4().hex[:12]}"
        edge = FieldLineageEdge(
            edge_id=edge_id,
            source_field_id=source_field_id,
            target_field_id=target_field_id,
            transformation=transformation,
            expression=expression,
        )
        self._edges[edge_id] = edge
        self._field_downstream.setdefault(source_field_id, set()).add(target_field_id)
        self._field_upstream.setdefault(target_field_id, set()).add(source_field_id)
        logger.info("添加字段血缘: %s -> %s (%s)", source_field_id, target_field_id, transformation)
        return edge

    def get_field(self, field_id: str) -> Optional[FieldNode]:
        """获取字段。"""
        return self._fields.get(field_id)

    def list_fields(self, dataset_id: Optional[str] = None) -> List[FieldNode]:
        """列出字段，可按数据集过滤。"""
        fields = list(self._fields.values())
        if dataset_id:
            fields = [f for f in fields if f.dataset_id == dataset_id]
        return fields

    def get_sources(self, field_id: str) -> List[str]:
        """获取字段的直接来源字段。"""
        return list(self._field_upstream.get(field_id, set()))

    def get_targets(self, field_id: str) -> List[str]:
        """获取字段的直接派生目标字段。"""
        return list(self._field_downstream.get(field_id, set()))

    def get_all_sources(self, field_id: str) -> List[str]:
        """递归获取字段的所有上游来源字段。"""
        visited: Set[str] = set()
        queue = [field_id]
        while queue:
            current = queue.pop(0)
            for prev in self._field_upstream.get(current, set()):
                if prev not in visited:
                    visited.add(prev)
                    queue.append(prev)
        return list(visited)

    def get_all_targets(self, field_id: str) -> List[str]:
        """递归获取字段的所有下游派生字段。"""
        visited: Set[str] = set()
        queue = [field_id]
        while queue:
            current = queue.pop(0)
            for nxt in self._field_downstream.get(current, set()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        return list(visited)

    def list_edges(self) -> List[FieldLineageEdge]:
        """列出所有字段血缘边。"""
        return list(self._edges.values())

    def find_pii_fields(self) -> List[FieldNode]:
        """查找所有标记为 PII 的字段。"""
        return [f for f in self._fields.values() if f.is_pii]

    def stats(self) -> Dict[str, Any]:
        """获取字段血缘统计信息。"""
        return {
            "fields": len(self._fields),
            "edges": len(self._edges),
            "pii_fields": len(self.find_pii_fields()),
        }


# ============================================================
# 影响分析
# ============================================================

class ImpactAnalyzer:
    """影响分析器 — 评估数据变更的影响范围，进行上游/下游影响分析。"""

    def __init__(self, tracker: DataFlowTracker, field_lineage: FieldLineage) -> None:
        self._tracker = tracker
        self._field_lineage = field_lineage

    def analyze_node(self, node_id: str) -> ImpactReport:
        """分析某个数据节点变更的影响范围。"""
        upstream = self._tracker.get_all_upstream(node_id)
        downstream = self._tracker.get_all_downstream(node_id)
        affected_fields: List[str] = []
        for field in self._field_lineage.list_fields(dataset_id=node_id):
            affected_fields.extend(self._field_lineage.get_all_targets(field.field_id))
            affected_fields.extend(self._field_lineage.get_all_sources(field.field_id))

        # 去重
        affected_fields = list(set(affected_fields))
        affected_datasets = list(set(upstream + downstream))

        severity = self._compute_severity(len(downstream), len(upstream))
        summary = (
            f"节点 {node_id} 变更将影响 {len(downstream)} 个下游数据集、"
            f"{len(upstream)} 个上游数据集、{len(affected_fields)} 个关联字段"
        )
        report = ImpactReport(
            changed_node_id=node_id,
            upstream=upstream,
            downstream=downstream,
            affected_datasets=affected_datasets,
            affected_fields=affected_fields,
            severity=severity,
            summary=summary,
        )
        logger.info("影响分析完成: %s (severity=%s)", node_id, severity)
        return report

    def analyze_field(self, field_id: str) -> ImpactReport:
        """分析某个字段变更的影响范围。"""
        field = self._field_lineage.get_field(field_id)
        node_id = field.dataset_id if field else field_id

        upstream_fields = self._field_lineage.get_all_sources(field_id)
        downstream_fields = self._field_lineage.get_all_targets(field_id)
        affected_fields = list(set(upstream_fields + downstream_fields))

        # 字段变更也会影响所在数据集的下游
        downstream = self._tracker.get_all_downstream(node_id) if field else []

        severity = self._compute_severity(len(downstream_fields), len(upstream_fields))
        summary = (
            f"字段 {field_id} 变更将影响 {len(downstream_fields)} 个下游字段、"
            f"{len(upstream_fields)} 个上游字段"
        )
        report = ImpactReport(
            changed_node_id=field_id,
            upstream=upstream_fields,
            downstream=downstream_fields,
            affected_datasets=downstream,
            affected_fields=affected_fields,
            severity=severity,
            summary=summary,
        )
        logger.info("字段影响分析完成: %s (severity=%s)", field_id, severity)
        return report

    @staticmethod
    def _compute_severity(downstream_count: int, upstream_count: int) -> str:
        """根据影响范围计算严重程度。"""
        total = downstream_count + upstream_count
        if total >= 20:
            return "critical"
        elif total >= 10:
            return "high"
        elif total >= 3:
            return "medium"
        return "low"


# ============================================================
# 合规审计
# ============================================================

class ComplianceAudit:
    """合规审计器 — 数据访问审计追踪，合规性检查（GDPR/数据保留策略）。"""

    def __init__(self) -> None:
        self._audit_entries: List[AuditEntry] = []
        self._retention_policies: Dict[str, RetentionPolicy] = {}
        self._max_entries = 10000  # 审计日志最大保留条数

    def record_access(
        self,
        actor: str,
        action: str,
        resource: str,
        success: bool = True,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AuditEntry:
        """记录一次数据访问审计条目。"""
        entry_id = f"aud_{uuid.uuid4().hex[:12]}"
        entry = AuditEntry(
            entry_id=entry_id,
            actor=actor,
            action=action,
            resource=resource,
            success=success,
            metadata=metadata or {},
        )
        self._audit_entries.append(entry)
        # 超出上限时丢弃最旧条目
        if len(self._audit_entries) > self._max_entries:
            self._audit_entries = self._audit_entries[-self._max_entries:]
        logger.info("审计记录: %s 对 %s 执行 %s (success=%s)", actor, resource, action, success)
        return entry

    def add_retention_policy(
        self,
        name: str,
        dataset_pattern: str,
        retention_days: int = 365,
        action_after: str = "archive",
        enabled: bool = True,
    ) -> RetentionPolicy:
        """添加数据保留策略。"""
        policy_id = f"rp_{uuid.uuid4().hex[:12]}"
        policy = RetentionPolicy(
            policy_id=policy_id,
            name=name,
            dataset_pattern=dataset_pattern,
            retention_days=retention_days,
            action_after=action_after,
            enabled=enabled,
        )
        self._retention_policies[policy_id] = policy
        logger.info("添加保留策略: %s (pattern=%s, days=%d)", name, dataset_pattern, retention_days)
        return policy

    def list_retention_policies(self) -> List[RetentionPolicy]:
        """列出所有保留策略。"""
        return list(self._retention_policies.values())

    def query_audit(
        self,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 100,
    ) -> List[AuditEntry]:
        """查询审计记录，支持多条件过滤。"""
        results = []
        for entry in reversed(self._audit_entries):
            if actor and entry.actor != actor:
                continue
            if action and entry.action != action:
                continue
            if resource and entry.resource != resource:
                continue
            if start_time and entry.timestamp < start_time:
                continue
            if end_time and entry.timestamp > end_time:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    def check_gdpr_compliance(
        self, field_lineage: FieldLineage, dataset_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """检查 GDPR 合规性 — 重点关注 PII 字段的访问与派生。"""
        pii_fields = field_lineage.find_pii_fields()
        if dataset_id:
            pii_fields = [f for f in pii_fields if f.dataset_id == dataset_id]

        # 检查 PII 字段是否被派生到非 PII 字段（潜在违规）
        violations: List[Dict[str, Any]] = []
        for pii in pii_fields:
            targets = field_lineage.get_all_targets(pii.field_id)
            for tid in targets:
                target_field = field_lineage.get_field(tid)
                if target_field and not target_field.is_pii:
                    violations.append({
                        "pii_field": pii.field_id,
                        "derived_field": tid,
                        "dataset": target_field.dataset_id,
                        "reason": "PII 字段派生到非 PII 字段，可能违反数据最小化原则",
                    })

        # 统计 PII 字段的访问记录
        pii_access: List[AuditEntry] = []
        for entry in self._audit_entries:
            for pii in pii_fields:
                if pii.field_name in entry.resource or pii.dataset_id in entry.resource:
                    pii_access.append(entry)
                    break

        compliant = len(violations) == 0
        result = {
            "compliant": compliant,
            "pii_field_count": len(pii_fields),
            "violation_count": len(violations),
            "violations": violations,
            "pii_access_count": len(pii_access),
            "checked_at": time.time(),
        }
        logger.info("GDPR 合规检查完成: compliant=%s, violations=%d", compliant, len(violations))
        return result

    def check_retention(
        self, datasets: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """检查数据保留策略合规性。

        ``datasets`` 为数据集列表，每项包含 ``name``、``created_at`` 等字段。
        """
        now = time.time()
        expired: List[Dict[str, Any]] = []
        for ds in datasets:
            name = ds.get("name", "")
            created_at = ds.get("created_at", now)
            age_days = (now - created_at) / 86400.0
            for policy in self._retention_policies.values():
                if not policy.enabled:
                    continue
                if fnmatch.fnmatch(name, policy.dataset_pattern):
                    if age_days > policy.retention_days:
                        expired.append({
                            "dataset": name,
                            "age_days": round(age_days, 1),
                            "policy": policy.name,
                            "retention_days": policy.retention_days,
                            "action_after": policy.action_after,
                        })
                    break

        result = {
            "compliant": len(expired) == 0,
            "checked_count": len(datasets),
            "expired_count": len(expired),
            "expired": expired,
            "checked_at": now,
        }
        logger.info("保留策略检查完成: expired=%d/%d", len(expired), len(datasets))
        return result

    def stats(self) -> Dict[str, Any]:
        """获取审计统计信息。"""
        return {
            "audit_entries": len(self._audit_entries),
            "retention_policies": len(self._retention_policies),
        }


# ============================================================
# 数据质量监控
# ============================================================

class DataQualityMonitor:
    """数据质量监控器 — 质量规则定义、质量评分、异常检测与告警。"""

    def __init__(self) -> None:
        self._rules: Dict[str, QualityRule] = {}
        self._reports: List[QualityReport] = []
        self._alert_handlers: List[Callable[[QualityReport], Any]] = []
        self._max_reports = 5000

    def add_rule(
        self,
        name: str,
        dataset_id: str,
        field_name: str = "",
        rule_type: str = "completeness",
        expression: str = "",
        threshold: float = 0.95,
        severity: str = "warning",
        enabled: bool = True,
    ) -> QualityRule:
        """添加数据质量规则。"""
        rule_id = f"qr_{uuid.uuid4().hex[:12]}"
        rule = QualityRule(
            rule_id=rule_id,
            name=name,
            dataset_id=dataset_id,
            field_name=field_name,
            rule_type=rule_type,
            expression=expression,
            threshold=threshold,
            severity=severity,
            enabled=enabled,
        )
        self._rules[rule_id] = rule
        logger.info("添加质量规则: %s (dataset=%s, type=%s)", name, dataset_id, rule_type)
        return rule

    def remove_rule(self, rule_id: str) -> bool:
        """移除质量规则。"""
        if rule_id in self._rules:
            del self._rules[rule_id]
            logger.info("移除质量规则: %s", rule_id)
            return True
        return False

    def list_rules(self, dataset_id: Optional[str] = None) -> List[QualityRule]:
        """列出质量规则，可按数据集过滤。"""
        rules = list(self._rules.values())
        if dataset_id:
            rules = [r for r in rules if r.dataset_id == dataset_id]
        return rules

    def add_alert_handler(self, handler: Callable[[QualityReport], Any]) -> None:
        """注册质量告警处理器。"""
        self._alert_handlers.append(handler)
        logger.info("注册质量告警处理器: %s", getattr(handler, "__name__", "anonymous"))

    def evaluate(
        self,
        rule_id: str,
        actual_value: float,
        expected_value: float = 1.0,
        message: str = "",
    ) -> Optional[QualityReport]:
        """评估单条质量规则并生成报告。

        ``actual_value`` 为实际测得的质量值（0.0 ~ 1.0），与阈值比较决定是否通过。
        """
        rule = self._rules.get(rule_id)
        if rule is None:
            logger.warning("质量规则不存在: %s", rule_id)
            return None
        if not rule.enabled:
            return None

        score = max(0.0, min(1.0, actual_value))
        passed = score >= rule.threshold
        report = QualityReport(
            report_id=f"qrep_{uuid.uuid4().hex[:12]}",
            dataset_id=rule.dataset_id,
            rule_id=rule_id,
            score=score,
            passed=passed,
            actual_value=actual_value,
            expected_value=expected_value,
            message=message or (rule.name if passed else f"{rule.name} 未通过阈值 {rule.threshold}"),
        )
        self._reports.append(report)
        if len(self._reports) > self._max_reports:
            self._reports = self._reports[-self._max_reports:]

        # 未通过则触发告警
        if not passed:
            logger.warning(
                "质量规则未通过: %s (score=%.3f, threshold=%.3f)",
                rule.name, score, rule.threshold,
            )
            self._fire_alert(report)
        else:
            logger.debug("质量规则通过: %s (score=%.3f)", rule.name, score)
        return report

    def _fire_alert(self, report: QualityReport) -> None:
        """触发质量告警，调用所有注册的告警处理器。"""
        for handler in self._alert_handlers:
            try:
                handler(report)
            except Exception as exc:
                logger.warning("质量告警处理器异常: %s", exc, exc_info=True)

    def detect_anomalies(self, dataset_id: str, window: int = 20) -> Dict[str, Any]:
        """异常检测 — 基于历史质量评分序列检测异常（z-score）。"""
        scores = [
            r.score for r in self._reports
            if r.dataset_id == dataset_id
        ][-window:]

        if len(scores) < 3:
            return {
                "dataset_id": dataset_id,
                "anomaly": False,
                "message": "样本不足，无法检测异常",
                "sample_count": len(scores),
            }

        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        std = variance ** 0.5
        latest = scores[-1]
        z_score = (latest - mean) / std if std > 0 else 0.0
        anomaly = abs(z_score) > 2.0  # z-score 超过 2 视为异常

        result = {
            "dataset_id": dataset_id,
            "anomaly": anomaly,
            "latest_score": latest,
            "mean": round(mean, 4),
            "std": round(std, 4),
            "z_score": round(z_score, 4),
            "sample_count": len(scores),
        }
        if anomaly:
            logger.warning("检测到质量异常: %s (z_score=%.2f)", dataset_id, z_score)
        return result

    def get_dataset_score(self, dataset_id: str) -> Dict[str, Any]:
        """获取数据集的综合质量评分。"""
        reports = [r for r in self._reports if r.dataset_id == dataset_id]
        if not reports:
            return {
                "dataset_id": dataset_id,
                "overall_score": 0.0,
                "report_count": 0,
                "pass_rate": 0.0,
            }
        overall = sum(r.score for r in reports) / len(reports)
        passed = sum(1 for r in reports if r.passed)
        return {
            "dataset_id": dataset_id,
            "overall_score": round(overall, 4),
            "report_count": len(reports),
            "pass_rate": round(passed / len(reports), 4),
        }

    def list_reports(self, dataset_id: Optional[str] = None, limit: int = 100) -> List[QualityReport]:
        """列出质量报告，可按数据集过滤。"""
        reports = list(reversed(self._reports))
        if dataset_id:
            reports = [r for r in reports if r.dataset_id == dataset_id]
        return reports[:limit]

    def stats(self) -> Dict[str, Any]:
        """获取质量监控统计信息。"""
        return {
            "rules": len(self._rules),
            "reports": len(self._reports),
            "alert_handlers": len(self._alert_handlers),
        }


# ============================================================
# 血缘可视化
# ============================================================

class LineageVisualizer:
    """血缘可视化器 — 生成 DAG 图数据，支持导出为 JSON/Graphviz 格式。"""

    def __init__(self, tracker: DataFlowTracker, field_lineage: FieldLineage) -> None:
        self._tracker = tracker
        self._field_lineage = field_lineage

    def build_dag(self) -> Dict[str, Any]:
        """构建数据流转 DAG 图数据（节点 + 边）。"""
        nodes = []
        edges = []

        # 数据节点
        for dn in self._tracker.list_data_nodes():
            nodes.append({
                "id": dn.node_id,
                "label": dn.name,
                "type": "data",
                "subtype": dn.node_type,
                "description": dn.description,
            })

        # 处理节点
        for pn_id in {fid for flow in self._tracker.list_flows() for fid in flow.processing_nodes}:
            pn = self._tracker.get_processing_node(pn_id)
            if pn:
                nodes.append({
                    "id": pn.node_id,
                    "label": pn.name,
                    "type": "processing",
                    "subtype": pn.operation,
                    "description": pn.description,
                })

        # 流转边
        for flow in self._tracker.list_flows():
            # 源 -> 第一个处理节点 -> ... -> 最后一个处理节点 -> 目标
            chain = [flow.source_id] + flow.processing_nodes + [flow.target_id]
            for i in range(len(chain) - 1):
                edges.append({
                    "id": f"{flow.flow_id}_{i}",
                    "source": chain[i],
                    "target": chain[i + 1],
                    "flow_id": flow.flow_id,
                })

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": self._tracker.stats(),
        }

    def build_field_dag(self, dataset_id: Optional[str] = None) -> Dict[str, Any]:
        """构建字段级血缘 DAG 图数据。"""
        nodes = []
        edges = []

        fields = self._field_lineage.list_fields(dataset_id=dataset_id)
        for f in fields:
            nodes.append({
                "id": f.field_id,
                "label": f.field_name,
                "type": "field",
                "dataset": f.dataset_id,
                "data_type": f.data_type,
                "is_pii": f.is_pii,
            })

        for edge in self._field_lineage.list_edges():
            edges.append({
                "id": edge.edge_id,
                "source": edge.source_field_id,
                "target": edge.target_field_id,
                "transformation": edge.transformation,
                "expression": edge.expression,
            })

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": self._field_lineage.stats(),
        }

    def export_json(self, dag: Optional[Dict[str, Any]] = None) -> str:
        """导出 DAG 图数据为 JSON 字符串。"""
        if dag is None:
            dag = self.build_dag()
        return json.dumps(dag, ensure_ascii=False, indent=2)

    def export_graphviz(self, dag: Optional[Dict[str, Any]] = None) -> str:
        """导出 DAG 图数据为 Graphviz DOT 格式字符串。"""
        if dag is None:
            dag = self.build_dag()

        lines = ["digraph lineage {", "  rankdir=LR;", "  node [fontname=\"Helvetica\"];"]

        # 节点形状与颜色按类型区分
        shape_map = {"data": "box", "processing": "ellipse", "field": "diamond"}
        color_map = {"data": "#4CAF50", "processing": "#2196F3", "field": "#FF9800"}

        for node in dag.get("nodes", []):
            nid = self._escape_dot(node["id"])
            label = self._escape_dot(node.get("label", node["id"]))
            ntype = node.get("type", "data")
            shape = shape_map.get(ntype, "box")
            color = color_map.get(ntype, "#4CAF50")
            lines.append(
                f'  "{nid}" [label="{label}", shape={shape}, color="{color}", style=filled];'
            )

        for edge in dag.get("edges", []):
            src = self._escape_dot(edge["source"])
            tgt = self._escape_dot(edge["target"])
            label = edge.get("transformation") or ""
            if label:
                lines.append(f'  "{src}" -> "{tgt}" [label="{self._escape_dot(label)}"];')
            else:
                lines.append(f'  "{src}" -> "{tgt}";')

        lines.append("}")
        return "\n".join(lines)

    def export_to_file(self, content: str, file_path: str) -> bool:
        """将内容导出到文件。"""
        try:
            path = Path(file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            logger.info("血缘图已导出到: %s", file_path)
            return True
        except Exception as exc:
            logger.warning("导出血缘图失败: %s", exc)
            return False

    @staticmethod
    def _escape_dot(text: str) -> str:
        """转义 DOT 格式中的特殊字符。"""
        return text.replace("\\", "\\\\").replace('"', '\\"')


# ============================================================
# 数据血缘插件
# ============================================================

class DataLineagePlugin(Plugin):
    """数据血缘与可观测性增强插件 — 整合流转追踪、字段血缘、影响分析、
    合规审计、质量监控与可视化能力。"""

    name = "data_lineage"
    depends_on: List[str] = []

    def __init__(self) -> None:
        super().__init__()
        self.flow_tracker = DataFlowTracker()
        self.field_lineage = FieldLineage()
        self.impact_analyzer = ImpactAnalyzer(self.flow_tracker, self.field_lineage)
        self.compliance_audit = ComplianceAudit()
        self.quality_monitor = DataQualityMonitor()
        self.visualizer = LineageVisualizer(self.flow_tracker, self.field_lineage)

    async def setup(self, ctx) -> None:
        """初始化数据血缘插件，从上下文加载配置。"""
        await super().setup(ctx)
        config = ctx.config if hasattr(ctx, "config") else {}
        lineage_cfg = config.get("data_lineage", {}) if isinstance(config, dict) else {}

        # 加载数据保留策略配置
        for policy_cfg in lineage_cfg.get("retention_policies", []):
            self.compliance_audit.add_retention_policy(
                name=policy_cfg.get("name", "default"),
                dataset_pattern=policy_cfg.get("dataset_pattern", "*"),
                retention_days=policy_cfg.get("retention_days", 365),
                action_after=policy_cfg.get("action_after", "archive"),
                enabled=policy_cfg.get("enabled", True),
            )

        # 加载数据质量规则配置
        for rule_cfg in lineage_cfg.get("quality_rules", []):
            self.quality_monitor.add_rule(
                name=rule_cfg.get("name", "default"),
                dataset_id=rule_cfg.get("dataset_id", ""),
                field_name=rule_cfg.get("field_name", ""),
                rule_type=rule_cfg.get("rule_type", "completeness"),
                expression=rule_cfg.get("expression", ""),
                threshold=rule_cfg.get("threshold", 0.95),
                severity=rule_cfg.get("severity", "warning"),
                enabled=rule_cfg.get("enabled", True),
            )

        logger.info(
            "data_lineage plugin configured (policies=%d, quality_rules=%d)",
            len(self.compliance_audit.list_retention_policies()),
            len(self.quality_monitor.list_rules()),
        )

    async def start(self) -> None:
        """启动数据血缘插件。"""
        await super().start()

    async def stop(self) -> None:
        """停止数据血缘插件。"""
        logger.info("data_lineage plugin stopped")

    # ---- 便捷方法：将各子能力暴露在插件层 ----

    def track_flow(
        self,
        source_name: str,
        target_name: str,
        processing_nodes: Optional[List[str]] = None,
    ) -> Optional[DataFlow]:
        """便捷方法：注册源/目标数据节点并记录一次流转。"""
        source = self.flow_tracker.register_data_node(source_name)
        target = self.flow_tracker.register_data_node(target_name)
        return self.flow_tracker.record_flow(
            source.node_id, target.node_id, processing_nodes
        )

    def analyze_impact(self, node_id: str) -> ImpactReport:
        """便捷方法：分析节点变更影响。"""
        return self.impact_analyzer.analyze_node(node_id)

    def visualize_json(self) -> str:
        """便捷方法：导出 JSON 格式血缘图。"""
        return self.visualizer.export_json()

    def visualize_graphviz(self) -> str:
        """便捷方法：导出 Graphviz 格式血缘图。"""
        return self.visualizer.export_graphviz()

    def get_overview(self) -> Dict[str, Any]:
        """获取数据血缘整体概览。"""
        return {
            "flow_tracker": self.flow_tracker.stats(),
            "field_lineage": self.field_lineage.stats(),
            "compliance_audit": self.compliance_audit.stats(),
            "quality_monitor": self.quality_monitor.stats(),
        }
