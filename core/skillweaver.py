"""SkillWeaver-style compositional skill routing.

Implements the three-phase pipeline from the Alibaba SkillWeaver paper:
1. Decompose: LLM breaks down user query into atomic subtasks
2. Retrieve: Embedding + FAISS semantic search for candidate skills
3. Compose: DAG workflow generation with parallel execution support

Plus the core innovation - SAD (Skill-Aware Decomposition) feedback loop:
    Generate → Retrieve → Reinject → Rewrite → (iterate until aligned)

This replaces the old pick_relevant() keyword matching with semantic search,
reducing token consumption by 99% while improving accuracy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================================
# Data Structures
# ============================================================

@dataclass
class SubTask:
    """A decomposed atomic subtask from user query."""
    id: str = ""
    description: str = ""
    original_description: str = ""  # Before SAD alignment
    candidate_skills: List[str] = field(default_factory=list)
    selected_skill: str = ""
    dependencies: List[str] = field(default_factory=list)  # IDs of subtasks this depends on
    status: str = "pending"  # pending, running, done, failed
    result: Any = None
    error: Optional[str] = None


@dataclass
class SkillNode:
    """A node in the DAG workflow."""
    subtask_id: str = ""
    skill_id: str = ""
    args: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)  # Skill node IDs
    status: str = "pending"
    result: Any = None


@dataclass
class DAGWorkflow:
    """Executable DAG workflow with parallel support."""
    nodes: List[SkillNode] = field(default_factory=list)
    edges: List[Tuple[str, str]] = field(default_factory=list)  # (from_id, to_id)
    entry_points: List[str] = field(default_factory=list)  # Nodes with no dependencies


# ============================================================
# Phase 1: Decompose
# ============================================================

DECOMPOSE_PROMPT_ZH = """【任务分解专家】

用户查询：{query}

请将这个查询拆解为 3-5 个原子子任务，每个子任务对应一个可执行工具。

要求：
1. 每个子任务必须是独立可执行的
2. 按执行顺序排列
3. 明确标注子任务之间的依赖关系（如果有的话）
4. 使用工具库的术语（如果已知）

输出 JSON 格式：
```json
{{
  "subtasks": [
    {{
      "id": "task_1",
      "description": "子任务描述",
      "dependencies": []
    }},
    {{
      "id": "task_2",
      "description": "子任务描述",
      "dependencies": ["task_1"]
    }}
  ]
}}
```

只输出 JSON，不要其他内容。"""

DECOMPOSE_PROMPT_EN = """【Task Decomposition Expert】

User query: {query}

Break down this query into 3-5 atomic subtasks, each corresponding to an executable tool.

Requirements:
1. Each subtask must be independently executable
2. Order by execution sequence
3. Clearly mark dependencies between subtasks (if any)
4. Use vocabulary from the tool library (if known)

Output JSON format:
```json
{{
  "subtasks": [
    {{
      "id": "task_1",
      "description": "subtask description",
      "dependencies": []
    }},
    {{
      "id": "task_2",
      "description": "subtask description",
      "dependencies": ["task_1"]
    }}
  ]
}}
```

Output only JSON, nothing else."""


# ============================================================
# SAD Feedback Loop
# ============================================================

SAD_REINJECT_PROMPT_ZH = """【技能对齐重写】

原始子任务：{original}
候选工具：{candidates}

上面是你原始分解的子任务，下面是工具库中检索到的候选工具描述。

请用工具库的术语重写子任务描述，使其与工具描述词汇对齐。
要求：
1. 保留原意，但使用工具库的术语
2. 保持简洁，不要过度解释
3. 如果候选工具不合适，保持原描述不变

只输出重写后的子任务描述，不要其他内容。"""

SAD_REINJECT_PROMPT_EN = """【Skill Alignment Rewrite】

Original subtask: {original}
Candidate tools: {candidates}

Above is your original subtask decomposition, below are candidate tool descriptions retrieved from the library.

Please rewrite the subtask description using tool library vocabulary for alignment.
Requirements:
1. Keep the original meaning but use tool library terminology
2. Stay concise, don't over-explain
3. If candidates don't fit, keep original description unchanged

Output only the rewritten subtask description, nothing else."""


# ============================================================
# Phase 3: Compose
# ============================================================

COMPOSE_PROMPT_ZH = """【工作流编排】

子任务序列：
{subtasks}

可用工具：
{tools}

请编排一个高效的 DAG 工作流：
1. 标注哪些子任务可以并行执行（无依赖）
2. 为每个子任务选择最合适的工具
3. 明确数据流向（前一步的输出如何传给下一步）

输出 JSON 格式：
```json
{{
  "nodes": [
    {{
      "subtask_id": "task_1",
      "skill_id": "web_search",
      "args": {{"input": "..."}},
      "dependencies": []
    }}
  ],
  "edges": [
    ["task_1", "task_2"]
  ]
}}
```

只输出 JSON，不要其他内容。"""

COMPOSE_PROMPT_EN = """【Workflow Composition】

Subtask sequence:
{subtasks}

Available tools:
{tools}

Compose an efficient DAG workflow:
1. Mark which subtasks can run in parallel (no dependencies)
2. Select the most appropriate tool for each subtask
3. Clarify data flow (how previous output passes to next step)

Output JSON format:
```json
{{
  "nodes": [
    {{
      "subtask_id": "task_1",
      "skill_id": "web_search",
      "args": {{"input": "..."}},
      "dependencies": []
    }}
  ],
  "edges": [
    ["task_1", "task_2"]
  ]
}}
```

Output only JSON, nothing else."""


# ============================================================
# Semantic Retrieval Layer
# ============================================================

class SkillIndex:
    """FAISS-based semantic skill index.
    
    Replaces keyword-based pick_relevant() with embedding search.
    Token reduction: from ~884,000 (all skill descriptions) to ~1,160 (top-K candidates).
    """
    
    def __init__(self, embedding_model: str = "all-MiniLM-L6-v2"):
        self._model_name = embedding_model
        self._model = None
        self._index = None
        self._skill_ids: List[str] = []
        self._skill_descriptions: List[str] = []
        self._built = False
        
    def _lazy_load(self):
        """Lazy load embedding model to avoid startup cost."""
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self._model_name)
                logger.info("SkillIndex: loaded embedding model %s", self._model_name)
            except ImportError:
                logger.warning(
                    "sentence-transformers not installed. Run: pip install sentence-transformers"
                )
                return False
        return True
    
    def build(self, skills: Dict[str, Any]) -> bool:
        """Build FAISS index from skill registry.
        
        Args:
            skills: Dict of skill_id -> Skill object
            
        Returns:
            True if index built successfully
        """
        if not self._lazy_load():
            return False
            
        try:
            import faiss
            import numpy as np
        except ImportError:
            logger.warning("faiss not installed. Run: pip install faiss-cpu")
            return False
        
        self._skill_ids = list(skills.keys())
        self._skill_descriptions = [
            f"{s.title} {s.description}" 
            for s in skills.values()
        ]
        
        if not self._skill_ids:
            logger.warning("SkillIndex: no skills to index")
            return False
        
        # Encode all skill descriptions (2209 skills ~15 seconds per paper)
        logger.info("SkillIndex: encoding %d skills...", len(self._skill_ids))
        start = time.time()
        embeddings = self._model.encode(self._skill_descriptions, show_progress_bar=False)
        embeddings = np.array(embeddings).astype('float32')
        
        # Build FAISS index (inner product for cosine similarity)
        self._index = faiss.IndexFlatIP(embeddings.shape[1])
        self._index.add(embeddings)
        
        self._built = True
        logger.info(
            "SkillIndex: built index for %d skills in %.2fs",
            len(self._skill_ids), time.time() - start
        )
        return True
    
    def retrieve(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Semantic retrieval of top-K skills for a query.
        
        Args:
            query: Subtask description or user query
            top_k: Number of candidates to retrieve
            
        Returns:
            List of (skill_id, score) tuples
        """
        if not self._built or not self._lazy_load():
            return []
        
        import numpy as np
        
        # Encode query
        query_vec = self._model.encode([query], show_progress_bar=False)
        query_vec = np.array(query_vec).astype('float32')
        
        # Search
        scores, indices = self._index.search(query_vec, top_k)
        
        results = []
        for i, idx in enumerate(indices[0]):
            if 0 <= idx < len(self._skill_ids):
                results.append((self._skill_ids[idx], float(scores[0][i])))
        
        return results
    
    def get_skill_description(self, skill_id: str) -> str:
        """Get the description for a skill."""
        if skill_id in self._skill_ids:
            idx = self._skill_ids.index(skill_id)
            return self._skill_descriptions[idx]
        return ""


# ============================================================
# SkillWeaver Router
# ============================================================

class SkillWeaverRouter:
    """Compositional skill routing with SAD feedback loop.
    
    Three-phase pipeline:
        Decompose → Retrieve → Compose
        
    SAD feedback loop runs between Decompose and Retrieve:
        1. LLM generates subtask descriptions
        2. Retrieve top-K candidates for each subtask
        3. Reinject candidate descriptions to LLM
        4. LLM rewrites subtask using tool vocabulary
        5. Iterate until aligned or max_iterations
    """
    
    def __init__(
        self,
        llm_provider,
        skill_manager,
        embedding_model: str = "all-MiniLM-L6-v2",
        max_sad_iterations: int = 2,
        top_k_candidates: int = 5,
    ):
        self._llm = llm_provider
        self._skills = skill_manager
        self._index = SkillIndex(embedding_model)
        self._max_sad_iter = max_sad_iterations
        self._top_k = top_k_candidates
        self._initialized = False
        
    def initialize(self) -> bool:
        """Build semantic index from current skill registry."""
        if self._initialized:
            return True

        # Get all skills from manager
        skills_dict = getattr(self._skills, '_skills', {})
        if not skills_dict:
            logger.warning("SkillWeaverRouter: no skills registered")
            return False

        # Build index
        self._initialized = self._index.build(skills_dict)
        return self._initialized

    def retrieve_skills(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """Public API: semantic retrieval of top-K skills for a query.

        Returns List of (skill_id, score) tuples.
        Falls back to empty list if index not available.
        """
        if not self._initialized:
            self.initialize()
        return self._index.retrieve(query, top_k)
    
    async def route(self, query: str, zh: bool = True) -> DAGWorkflow:
        """Main entry point: decompose → retrieve → compose with SAD loop.
        
        Args:
            query: User input text
            zh: Use Chinese prompts
            
        Returns:
            DAGWorkflow ready for execution
        """
        if not self._initialized:
            self.initialize()
        
        # Phase 1: Decompose
        subtasks = await self._decompose(query, zh)
        if not subtasks:
            return DAGWorkflow()
        
        # SAD Feedback Loop
        subtasks = await self._sad_feedback_loop(subtasks, zh)
        
        # Phase 2: Retrieve (already done in SAD loop)
        
        # Phase 3: Compose
        workflow = await self._compose(subtasks, zh)
        
        return workflow
    
    async def _decompose(self, query: str, zh: bool) -> List[SubTask]:
        """Phase 1: LLM decomposes query into atomic subtasks."""
        prompt = (DECOMPOSE_PROMPT_ZH if zh else DECOMPOSE_PROMPT_EN).format(query=query)
        
        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=None,  # Use default
                max_tokens=500,
                tools=None,
            )
            
            text = resp.get("text", "")
            # Extract JSON
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]
            
            data = json.loads(text.strip())
            subtasks = []
            for item in data.get("subtasks", []):
                subtasks.append(SubTask(
                    id=item.get("id", ""),
                    description=item.get("description", ""),
                    original_description=item.get("description", ""),
                    dependencies=item.get("dependencies", []),
                ))
            return subtasks
            
        except Exception as exc:
            logger.error("SkillWeaver decompose failed: %s", exc)
            return []
    
    async def _sad_feedback_loop(self, subtasks: List[SubTask], zh: bool) -> List[SubTask]:
        """SAD: iterate until subtask vocabulary aligns with tool library."""
        for iteration in range(self._max_sad_iter):
            all_aligned = True
            
            for subtask in subtasks:
                # Retrieve candidates
                candidates = self._index.retrieve(subtask.description, self._top_k)
                
                if not candidates:
                    continue
                
                # Get candidate descriptions
                candidate_descs = []
                for skill_id, score in candidates[:3]:  # Top-3
                    desc = self._index.get_skill_description(skill_id)
                    candidate_descs.append(f"- {skill_id}: {desc[:100]}")
                
                # Reinject and rewrite
                prompt = (SAD_REINJECT_PROMPT_ZH if zh else SAD_REINJECT_PROMPT_EN).format(
                    original=subtask.description,
                    candidates="\n".join(candidate_descs),
                )
                
                try:
                    resp = await self._llm.chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        model=None,
                        max_tokens=200,
                        tools=None,
                    )
                    
                    rewritten = resp.get("text", "").strip()
                    if rewritten and rewritten != subtask.description:
                        subtask.description = rewritten
                        subtask.candidate_skills = [c[0] for c in candidates]
                        all_aligned = False
                        
                except Exception as exc:
                    logger.debug("SAD rewrite failed: %s", exc)
            
            if all_aligned:
                logger.debug("SAD: all subtasks aligned after %d iterations", iteration + 1)
                break
        
        return subtasks
    
    async def _compose(self, subtasks: List[SubTask], zh: bool) -> DAGWorkflow:
        """Phase 3: Compose DAG workflow from aligned subtasks."""
        # Build subtask list string
        subtask_list = []
        for st in subtasks:
            candidates = st.candidate_skills[:3] if st.candidate_skills else []
            subtask_list.append(
                f"- {st.id}: {st.description} (candidates: {', '.join(candidates)})"
            )
        
        # Get available tools
        skills_dict = getattr(self._skills, '_skills', {})
        tool_list = [f"- {sid}: {s.description[:80]}" for sid, s in skills_dict.items()][:20]
        
        prompt = (COMPOSE_PROMPT_ZH if zh else COMPOSE_PROMPT_EN).format(
            subtasks="\n".join(subtask_list),
            tools="\n".join(tool_list),
        )
        
        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=None,
                max_tokens=800,
                tools=None,
            )
            
            text = resp.get("text", "")
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]
            
            data = json.loads(text.strip())
            
            nodes = []
            for node_data in data.get("nodes", []):
                nodes.append(SkillNode(
                    subtask_id=node_data.get("subtask_id", ""),
                    skill_id=node_data.get("skill_id", ""),
                    args=node_data.get("args", {}),
                    dependencies=node_data.get("dependencies", []),
                ))
            
            edges = [tuple(e) for e in data.get("edges", [])]
            
            # Find entry points (nodes with no dependencies)
            all_deps = set()
            for node in nodes:
                all_deps.update(node.dependencies)
            entry_points = [n.subtask_id for n in nodes if n.subtask_id not in all_deps]
            
            return DAGWorkflow(
                nodes=nodes,
                edges=edges,
                entry_points=entry_points,
            )
            
        except Exception as exc:
            logger.error("SkillWeaver compose failed: %s", exc)
            return DAGWorkflow()
    
    async def execute_workflow(
        self,
        workflow: DAGWorkflow,
        on_progress: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, Any]:
        """Execute DAG workflow with parallel support.

        Args:
            workflow: DAGWorkflow to execute
            on_progress: Optional callback for progress updates

        Returns:
            Dict with final result and execution metadata
        """
        if not workflow.nodes:
            return {"error": "empty workflow"}

        # 修复：先做静态环检测 — LLM 在 _compose 阶段可能生成循环依赖（如 A→B→A）
        # 之前的 while 循环会永远等不到 completed
        if self._has_cycle(workflow):
            logger.error("SkillWeaver: detected cycle in workflow, aborting")
            return {"error": "workflow has cycle (circular dependencies)"}

        # 修复：等依赖时加超时（默认 30s），避免某个节点卡死时整个 turn hang
        dep_wait_timeout_s = 30.0

        results: Dict[str, Any] = {}
        completed: set = set()

        async def execute_node(node: SkillNode) -> None:
            """Execute a single node, respecting dependencies."""
            # Wait for dependencies — 加超时保护
            deadline = asyncio.get_running_loop().time() + dep_wait_timeout_s
            while not all(d in completed for d in node.dependencies):
                if asyncio.get_running_loop().time() >= deadline:
                    logger.error("SkillWeaver: node %s timed out waiting for deps %s",
                                node.subtask_id, node.dependencies)
                    node.status = "failed"
                    node.error = f"dependency wait timeout ({dep_wait_timeout_s}s)"
                    results[node.subtask_id] = f"Error: {node.error}"
                    completed.add(node.subtask_id)
                    if on_progress:
                        on_progress(node.subtask_id, "failed")
                    return
                await asyncio.sleep(0.1)

            # Execute skill
            if on_progress:
                on_progress(node.subtask_id, "running")

            try:
                result = await self._skills.dispatch(node.skill_id, node.args)
                node.result = result
                node.status = "done"
                results[node.subtask_id] = result

                if on_progress:
                    on_progress(node.subtask_id, "done")

            except Exception as exc:
                node.status = "failed"
                node.error = str(exc)
                results[node.subtask_id] = f"Error: {exc}"

                if on_progress:
                    on_progress(node.subtask_id, "failed")

            completed.add(node.subtask_id)

        # Execute all nodes in parallel (dependencies handled internally)
        tasks = [execute_node(node) for node in workflow.nodes]
        await asyncio.gather(*tasks)

        return {
            "results": results,
            "nodes": workflow.nodes,
            "success": all(n.status == "done" for n in workflow.nodes),
        }

    @staticmethod
    def _has_cycle(workflow: DAGWorkflow) -> bool:
        """Detect cycle in DAG using DFS coloring. O(V+E)."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n.subtask_id: WHITE for n in workflow.nodes}
        # 建邻接表
        adj: Dict[str, List[str]] = {n.subtask_id: list(n.dependencies) for n in workflow.nodes}

        def dfs(node_id: str) -> bool:
            color[node_id] = GRAY
            for dep in adj.get(node_id, []):
                if dep not in color:
                    # 依赖指向不存在的节点 — 视为坏图，跳过
                    continue
                if color[dep] == GRAY:
                    return True  # back edge → cycle
                if color[dep] == WHITE and dfs(dep):
                    return True
            color[node_id] = BLACK
            return False

        for nid in color:
            if color[nid] == WHITE and dfs(nid):
                return True
        return False


# ============================================================
# Integration Helper
# ============================================================

def create_skillweaver_router(llm_provider, skill_manager) -> SkillWeaverRouter:
    """Factory function to create a SkillWeaverRouter.
    
    Usage in coordinator:
        from core.skillweaver import create_skillweaver_router
        
        router = create_skillweaver_router(self._llm, self._skills)
        workflow = await router.route(turn.input_text)
        result = await router.execute_workflow(workflow)
    """
    return SkillWeaverRouter(llm_provider, skill_manager)