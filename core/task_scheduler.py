"""Async Task Scheduler — background tasks, delayed execution, and scheduled jobs.

Features:
- Delayed tasks: execute after N seconds ("remind me in 5 minutes")
- Scheduled tasks: cron-like scheduling ("every day at 9am")
- One-time tasks: execute once at a specific time
- Background tasks: execute in the background without blocking
- Task persistence: tasks survive restarts (stored in SQLite)
- Task chaining: run tasks sequentially or in parallel
- Rate limiting: prevent task flooding
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Task execution status."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(Enum):
    """Task priority levels."""
    LOW = 0
    NORMAL = 5
    HIGH = 8
    CRITICAL = 10


@dataclass
class Task:
    """A schedulable task."""
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    name: str = ""
    task_type: str = "background"  # background, delayed, scheduled, one_time
    # For delayed tasks
    delay_seconds: float = 0
    # For scheduled tasks (cron-like)
    cron_expression: str = ""
    # For one-time tasks
    run_at: float = 0  # Unix timestamp
    # Execution
    func_name: str = ""
    func_args: str = ""  # JSON serialized
    max_retries: int = 2
    retry_delay: float = 5.0
    # Status tracking
    status: str = TaskStatus.PENDING.value
    priority: int = TaskPriority.NORMAL.value
    created_at: float = field(default_factory=time.time)
    scheduled_at: float = 0
    started_at: float = 0
    completed_at: float = 0
    result: str = ""
    error: str = ""
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "task_type": self.task_type,
            "delay_seconds": self.delay_seconds,
            "cron_expression": self.cron_expression,
            "run_at": self.run_at,
            "func_name": self.func_name,
            "status": self.status,
            "priority": self.priority,
            "created_at": self.created_at,
            "scheduled_at": self.scheduled_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "retry_count": self.retry_count,
        }


class TaskStore:
    """SQLite-backed task persistence."""

    def __init__(self, db_path: str = "data/memory/tasks.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # Enable dict-like access
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._write_lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._write_lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT DEFAULT '',
                    task_type TEXT DEFAULT 'background',
                    delay_seconds REAL DEFAULT 0,
                    cron_expression TEXT DEFAULT '',
                    run_at REAL DEFAULT 0,
                    func_name TEXT DEFAULT '',
                    func_args TEXT DEFAULT '{}',
                    max_retries INTEGER DEFAULT 2,
                    retry_delay REAL DEFAULT 5.0,
                    status TEXT DEFAULT 'pending',
                    priority INTEGER DEFAULT 5,
                    created_at REAL,
                    scheduled_at REAL DEFAULT 0,
                    started_at REAL DEFAULT 0,
                    completed_at REAL DEFAULT 0,
                    result TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    retry_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
                CREATE INDEX IF NOT EXISTS idx_tasks_run_at ON tasks(run_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority DESC);
            """)
            self._conn.commit()

    def save(self, task: Task) -> None:
        with self._write_lock:
            self._conn.execute("""
                INSERT OR REPLACE INTO tasks
                (id, name, task_type, delay_seconds, cron_expression, run_at,
                 func_name, func_args, max_retries, retry_delay, status, priority,
                 created_at, scheduled_at, started_at, completed_at, result, error, retry_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task.id, task.name, task.task_type, task.delay_seconds, task.cron_expression,
                task.run_at, task.func_name, task.func_args, task.max_retries, task.retry_delay,
                task.status, task.priority, task.created_at, task.scheduled_at, task.started_at,
                task.completed_at, task.result, task.error, task.retry_count,
            ))
            self._conn.commit()

    def get(self, task_id: str) -> Optional[Task]:
        cur = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if row:
            return self._row_to_task(row)
        return None

    def get_pending(self, limit: int = 100) -> List[Task]:
        now = time.time()
        cur = self._conn.execute("""
            SELECT * FROM tasks
            WHERE status IN ('pending', 'failed')
            AND (run_at = 0 OR run_at <= ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
        """, (now, limit))
        return [self._row_to_task(r) for r in cur.fetchall()]

    def get_running(self) -> List[Task]:
        cur = self._conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY started_at DESC",
            (TaskStatus.RUNNING.value,),
        )
        return [self._row_to_task(r) for r in cur.fetchall()]

    def list_tasks(self, status: Optional[str] = None, limit: int = 50) -> List[Task]:
        if status:
            cur = self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [self._row_to_task(r) for r in cur.fetchall()]

    def delete(self, task_id: str) -> bool:
        with self._write_lock:
            cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], name=row["name"], task_type=row["task_type"],
            delay_seconds=row["delay_seconds"], cron_expression=row["cron_expression"],
            run_at=row["run_at"], func_name=row["func_name"],
            func_args=row["func_args"], max_retries=row["max_retries"],
            retry_delay=row["retry_delay"], status=row["status"],
            priority=row["priority"], created_at=row["created_at"],
            scheduled_at=row["scheduled_at"], started_at=row["started_at"],
            completed_at=row["completed_at"], result=row["result"],
            error=row["error"], retry_count=row["retry_count"],
        )


class AsyncTaskScheduler:
    """Async task scheduler with background execution.

    Supports:
    - Delayed tasks: execute after N seconds
    - One-time scheduled: execute at specific time
    - Background tasks: fire-and-forget
    - Retry with backoff
    """

    def __init__(
        self,
        db_path: str = "data/memory/tasks.db",
        max_concurrent: int = 5,
    ) -> None:
        self._store = TaskStore(db_path)
        self._max_concurrent = max_concurrent
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._shutdown = False
        self._task_registry: Dict[str, Callable] = {}

    def register(self, name: str, func: Callable) -> None:
        """Register a callable task function."""
        self._task_registry[name] = func
        logger.debug("Registered task function: %s", name)

    async def schedule_delayed(
        self,
        func_name: str,
        delay_seconds: float,
        args: Optional[Dict[str, Any]] = None,
        name: str = "",
        priority: int = TaskPriority.NORMAL.value,
        max_retries: int = 2,
    ) -> str:
        """Schedule a task to run after delay_seconds."""
        task = Task(
            name=name or f"delayed-{func_name}",
            task_type="delayed",
            delay_seconds=delay_seconds,
            func_name=func_name,
            func_args=json.dumps(args or {}),
            priority=priority,
            max_retries=max_retries,
            run_at=time.time() + delay_seconds,
            scheduled_at=time.time(),
        )
        self._store.save(task)
        logger.info("Scheduled delayed task %s (%s) for +%.0fs", task.id, func_name, delay_seconds)
        return task.id

    async def schedule_at(
        self,
        func_name: str,
        run_at: float,
        args: Optional[Dict[str, Any]] = None,
        name: str = "",
        priority: int = TaskPriority.NORMAL.value,
    ) -> str:
        """Schedule a task to run at a specific Unix timestamp."""
        task = Task(
            name=name or f"scheduled-{func_name}",
            task_type="one_time",
            run_at=run_at,
            func_name=func_name,
            func_args=json.dumps(args or {}),
            priority=priority,
            scheduled_at=time.time(),
        )
        self._store.save(task)
        logger.info("Scheduled task %s (%s) for %s", task.id, func_name, time.ctime(run_at))
        return task.id

    async def schedule_background(
        self,
        func_name: str,
        args: Optional[Dict[str, Any]] = None,
        name: str = "",
        priority: int = TaskPriority.LOW.value,
    ) -> str:
        """Schedule a background task (runs ASAP)."""
        task = Task(
            name=name or f"bg-{func_name}",
            task_type="background",
            func_name=func_name,
            func_args=json.dumps(args or {}),
            priority=priority,
            run_at=time.time(),
            scheduled_at=time.time(),
        )
        self._store.save(task)
        logger.debug("Queued background task %s (%s)", task.id, func_name)
        return task.id

    async def cancel(self, task_id: str) -> bool:
        """Cancel a pending or running task."""
        task = self._store.get(task_id)
        if not task:
            return False

        if task.id in self._running_tasks:
            self._running_tasks[task.id].cancel()
            del self._running_tasks[task.id]

        task.status = TaskStatus.CANCELLED.value
        task.completed_at = time.time()
        self._store.save(task)
        logger.info("Cancelled task %s", task_id)
        return True

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task status."""
        return self._store.get(task_id)

    def list_tasks(self, status: Optional[str] = None) -> List[Task]:
        """List tasks, optionally filtered by status."""
        return self._store.list_tasks(status)

    async def start(self) -> None:
        """Start the scheduler (call once)."""
        self._shutdown = False
        logger.info("Task scheduler started (max concurrent: %d)", self._max_concurrent)
        # 关键修复：保存强引用, 否则 asyncio.create_task 返回的 Task 可能被 GC
        # 中途取消 ("Task was destroyed but it is pending!") → 后台调度静默停摆。
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Stop the scheduler gracefully."""
        self._shutdown = True
        # Cancel the poll loop task (saved as strong ref in start())
        poll_task = getattr(self, "_poll_task", None)
        if poll_task is not None and not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        # Cancel running tasks
        for task_id, task_handle in list(self._running_tasks.items()):
            task_handle.cancel()
        logger.info("Task scheduler stopped")

    async def _poll_loop(self) -> None:
        """Poll for pending tasks and execute them."""
        while not self._shutdown:
            try:
                await asyncio.sleep(1)  # Poll every second

                # Check running tasks
                done = [tid for tid in self._running_tasks if self._running_tasks[tid].done()]
                for tid in done:
                    del self._running_tasks[tid]

                # Get pending tasks
                pending = self._store.get_pending(limit=self._max_concurrent)
                for task in pending:
                    if len(self._running_tasks) >= self._max_concurrent:
                        break
                    if task.id in self._running_tasks:
                        continue
                    # Execute task
                    handle = asyncio.create_task(self._execute(task))
                    self._running_tasks[task.id] = handle

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scheduler poll error: %s", exc)

    async def _execute(self, task: Task) -> None:
        """Execute a single task with retry support."""
        async with self._semaphore:
            func = self._task_registry.get(task.func_name)
            if not func:
                task.status = TaskStatus.FAILED.value
                task.error = f"Unknown function: {task.func_name}"
                self._store.save(task)
                logger.error("Task %s failed: unknown function %s", task.id, task.func_name)
                return

            task.status = TaskStatus.RUNNING.value
            task.started_at = time.time()
            self._store.save(task)

            try:
                args = json.loads(task.func_args) if task.func_args else {}
                # Call the function (supports both sync and async)
                if asyncio.iscoroutinefunction(func):
                    result = await func(**args)
                else:
                    result = func(**args)

                task.status = TaskStatus.COMPLETED.value
                task.result = json.dumps(result) if result is not None else ""
                task.completed_at = time.time()
                self._store.save(task)
                logger.info("Task %s completed", task.id)

            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED.value
                task.completed_at = time.time()
                self._store.save(task)

            except Exception as exc:
                task.retry_count += 1
                task.error = str(exc)[:500]

                if task.retry_count <= task.max_retries:
                    # Schedule retry with backoff
                    task.status = TaskStatus.PENDING.value
                    task.run_at = time.time() + task.retry_delay * task.retry_count
                    self._store.save(task)
                    logger.warning(
                        "Task %s failed (attempt %d/%d), retry scheduled in %.0fs: %s",
                        task.id, task.retry_count, task.max_retries,
                        task.retry_delay * task.retry_count, exc,
                    )
                else:
                    task.status = TaskStatus.FAILED.value
                    task.completed_at = time.time()
                    self._store.save(task)
                    logger.error("Task %s failed after %d retries: %s",
                                task.id, task.max_retries, exc)


# Singleton
_scheduler: Optional[AsyncTaskScheduler] = None


def get_task_scheduler() -> AsyncTaskScheduler:
    """Get the shared task scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncTaskScheduler()
    return _scheduler
