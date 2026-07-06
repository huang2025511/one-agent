"""Batch Processing — map-reduce for parallel task execution.

Provides:
  - Map: split a batch task into individual items
  - Execute: run each item in parallel with concurrency control
  - Reduce: combine results into a unified output
  - Progress tracking for each item
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class BatchItemStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class BatchItem:
    """A single item in a batch task."""
    item_id: int
    input_data: str
    status: BatchItemStatus = BatchItemStatus.PENDING
    result: str = ""
    error: str = ""
    duration_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchResult:
    """Result of a batch processing operation."""
    items: List[BatchItem] = field(default_factory=list)
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    total_duration_ms: float = 0.0
    combined_result: str = ""


class BatchProcessor:
    """Processes multiple items in parallel with map-reduce pattern.

    Usage:
        processor = BatchProcessor(llm, skills)
        result = await processor.process(
            items=["translate 'hello' to French", "translate 'goodbye' to French"],
            task_type="translate",
            max_concurrency=3,
        )
    """

    DEFAULT_MAX_CONCURRENCY = 5
    DEFAULT_TIMEOUT_PER_ITEM = 60.0

    def __init__(self, llm_provider, skills_manager=None):
        self._llm = llm_provider
        self._skills = skills_manager
        self._max_concurrency = self.DEFAULT_MAX_CONCURRENCY

    # --------------------------------------------------- map phase

    def split_items(self, text: str, delimiter: str = "\n") -> List[str]:
        """Split a batch input into individual items.

        Handles common patterns:
        - Newline-separated list
        - Numbered list ("1. xxx", "2. yyy")
        - Comma-separated items
        """
        items = []

        # Try numbered list first
        import re
        numbered = re.findall(r"(?:^|\n)\s*\d+[\.\)、]\s*(.+)", text)
        if len(numbered) >= 2:
            items = [n.strip() for n in numbered if n.strip()]
            return items

        # Try newline-separated
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) >= 2:
            # Check if comma-separated within single line
            if len(lines) == 1 and "," in lines[0]:
                items = [i.strip() for i in lines[0].split(",") if i.strip()]
            else:
                items = lines
            return items

        # Fallback: single item
        return [text.strip()] if text.strip() else []

    # --------------------------------------------------- execute phase

    async def process(
        self,
        items: List[str],
        model: Optional[str] = None,
        task_type: str = "general",
        max_concurrency: Optional[int] = None,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        temperature: float = 0.3,
    ) -> BatchResult:
        """Process a batch of items in parallel.

        Args:
            items: list of item strings to process
            model: LLM model to use
            task_type: type of task (translate, summarize, classify, etc.)
            max_concurrency: max parallel items (default: 5)
            on_progress: optional callback(done, total, current_item)
            temperature: LLM temperature for batch processing
        """
        if not items:
            return BatchResult(items=[], total=0)

        concurrency = max_concurrency or self.DEFAULT_MAX_CONCURRENCY
        sem = asyncio.Semaphore(concurrency)

        batch_items = [
            BatchItem(item_id=i, input_data=item)
            for i, item in enumerate(items)
        ]
        total = len(batch_items)

        start = time.time()

        async def process_one(item: BatchItem) -> None:
            async with sem:
                item.status = BatchItemStatus.RUNNING
                item_start = time.time()
                try:
                    result = await asyncio.wait_for(
                        self._process_single(item.input_data, model, task_type, temperature),
                        timeout=self.DEFAULT_TIMEOUT_PER_ITEM,
                    )
                    item.result = result
                    item.status = BatchItemStatus.DONE
                except asyncio.TimeoutError:
                    item.error = "timeout"
                    item.status = BatchItemStatus.FAILED
                except Exception as exc:
                    item.error = str(exc)[:200]
                    item.status = BatchItemStatus.FAILED
                finally:
                    item.duration_ms = (time.time() - item_start) * 1000

                if on_progress:
                    done = sum(1 for i in batch_items if i.status in (BatchItemStatus.DONE, BatchItemStatus.FAILED))
                    try:
                        on_progress(done, total, item.input_data[:50])
                    except Exception:
                        pass

        # Run all in parallel (limited by semaphore)
        await asyncio.gather(*(process_one(item) for item in batch_items))

        succeeded = sum(1 for i in batch_items if i.status == BatchItemStatus.DONE)
        failed = sum(1 for i in batch_items if i.status == BatchItemStatus.FAILED)

        # Reduce: combine results
        combined = await self._reduce(batch_items, task_type, model)

        return BatchResult(
            items=batch_items,
            total=total,
            succeeded=succeeded,
            failed=failed,
            total_duration_ms=(time.time() - start) * 1000,
            combined_result=combined,
        )

    async def _process_single(
        self, item: str, model: Optional[str], task_type: str, temperature: float,
    ) -> str:
        """Process a single batch item via LLM."""
        prompts = {
            "translate": f"将以下内容翻译成中文，只输出翻译结果：\n{item}",
            "summarize": f"用一句话总结以下内容：\n{item}",
            "classify": f"将以下内容分类（如：技术/生活/工作/娱乐），只输出类别名：\n{item}",
            "extract": f"从以下内容中提取关键信息（人名、日期、数字等），简洁输出：\n{item}",
            "general": item,
        }

        prompt = prompts.get(task_type, item)

        resp = await self._llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=temperature,
            max_tokens=500,
            tools=None,
            use_cache=False,
        )
        return (resp.get("text") or "").strip()

    # --------------------------------------------------- reduce phase

    async def _reduce(
        self, items: List[BatchItem], task_type: str, model: Optional[str],
    ) -> str:
        """Combine individual results into a unified output."""
        succeeded_items = [i for i in items if i.status == BatchItemStatus.DONE]
        if not succeeded_items:
            return "（所有项目处理失败）"

        if len(succeeded_items) == 1:
            return succeeded_items[0].result

        # For multiple items, synthesize a combined result
        items_text = ""
        for item in succeeded_items:
            items_text += f"\n--- 项目 {item.item_id + 1} ---\n{item.result[:300]}"

        reduce_prompt = (
            f"以下是 {len(succeeded_items)} 个已处理的项目结果。"
            f"请将它们整合成一个简洁的汇总输出"
            + (f"（任务类型：{task_type}）" if task_type != "general" else "")
            + f"：\n{items_text[:6000]}\n\n汇总输出："
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": reduce_prompt}],
                model=model,
                temperature=0.3,
                max_tokens=1000,
                tools=None,
                use_cache=False,
            )
            return (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("batch reduce failed: %s", exc)
            # Fallback: simple concatenation
            return "\n\n".join(
                f"{i+1}. {item.result[:200]}"
                for i, item in enumerate(succeeded_items)
            )

    # --------------------------------------------------- utilities

    def format_result(self, result: BatchResult) -> str:
        """Format a BatchResult for display."""
        lines = [
            f"批量处理完成：{result.succeeded}/{result.total} 成功",
        ]
        if result.failed > 0:
            lines.append(f"  {result.failed} 项失败")
        lines.append(f"  耗时: {result.total_duration_ms / 1000:.1f}s")
        lines.append("")
        lines.append(result.combined_result)
        return "\n".join(lines)


# Singleton
_batch_processor: Optional[BatchProcessor] = None


def get_batch_processor(llm=None, skills=None) -> BatchProcessor:
    """Get or create the shared BatchProcessor."""
    global _batch_processor
    if _batch_processor is None and llm is not None:
        _batch_processor = BatchProcessor(llm, skills)
    elif _batch_processor is None:
        _batch_processor = BatchProcessor(None, None)
    return _batch_processor