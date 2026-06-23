import asyncio
import sqlite3
import uuid
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, List
from enum import Enum


class TaskStatus(Enum):
    """任务状态枚举"""
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


class TaskPriority(Enum):
    """任务优先级枚举"""
    HIGH = 'high'
    MEDIUM = 'medium'
    LOW = 'low'

    def to_weight(self) -> int:
        """转换为权重值，用于排序"""
        weights = {'high': 0, 'medium': 1, 'low': 2}
        return weights[self.value]


@dataclass
class Task:
    """任务数据类"""
    task_id: str
    name: str
    status: str
    progress: int = 0
    result: Any = None
    error: str = ''
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    retry_count: int = 0
    max_retries: int = 3
    priority: str = 'medium'
    args: Dict[str, Any] = None

    def __post_init__(self):
        """初始化后处理"""
        if self.args is None:
            self.args = {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于持久化"""
        return {
            'task_id': self.task_id,
            'name': self.name,
            'status': self.status,
            'progress': self.progress,
            'result': json.dumps(self.result) if self.result is not None else None,
            'error': self.error,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'retry_count': self.retry_count,
            'max_retries': self.max_retries,
            'priority': self.priority,
            'args': json.dumps(self.args) if self.args else '{}'
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Task':
        """从字典创建 Task 对象"""
        result = data.get('result')
        args_data = data.get('args')
        return cls(
            task_id=data['task_id'],
            name=data['name'],
            status=data['status'],
            progress=data['progress'],
            result=json.loads(result) if result else None,
            error=data['error'],
            created_at=data['created_at'],
            started_at=data['started_at'],
            completed_at=data['completed_at'],
            retry_count=data['retry_count'],
            max_retries=data['max_retries'],
            priority=data.get('priority', 'medium'),
            args=json.loads(args_data) if args_data else {}
        )


class TaskQueue:
    """异步任务队列类"""

    def __init__(self, db_path: str = 'tasks.db'):
        """
        初始化任务队列
        
        Args:
            db_path: SQLite 数据库文件路径
        """
        self.db_path = db_path
        self._init_db()
        self._running = False
        self._workers = []
        self._lock = asyncio.Lock()
        self._progress_callbacks: Dict[str, List[Callable]] = {}
        self._task_handlers: Dict[str, Callable] = {}
        self._timeout = 300  # 默认超时时间（秒）

    def _init_db(self) -> None:
        """初始化数据库表"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    progress INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    error TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL DEFAULT 0,
                    completed_at REAL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    priority TEXT NOT NULL DEFAULT 'medium',
                    args TEXT NOT NULL DEFAULT '{}'
                )
            ''')
            conn.commit()

    async def submit(self, name: str, args: Dict[str, Any] = None, 
                    priority: str = 'medium', max_retries: int = 3) -> str:
        """
        提交任务到队列
        
        Args:
            name: 任务名称
            args: 任务参数
            priority: 优先级（high/medium/low）
            max_retries: 最大重试次数
        
        Returns:
            task_id: 任务ID
        """
        task_id = str(uuid.uuid4())
        now = time.time()
        
        task = Task(
            task_id=task_id,
            name=name,
            status=TaskStatus.PENDING.value,
            created_at=now,
            max_retries=max_retries
        )

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO tasks (task_id, name, status, progress, result, error,
                                created_at, started_at, completed_at, retry_count,
                                max_retries, priority, args)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                task_id,
                name,
                task.status,
                task.progress,
                None,
                task.error,
                task.created_at,
                task.started_at,
                task.completed_at,
                task.retry_count,
                task.max_retries,
                priority,
                json.dumps(args or {})
            ))
            conn.commit()

        return task_id

    def get_task(self, task_id: str) -> Optional[Task]:
        """
        根据任务ID查询任务
        
        Args:
            task_id: 任务ID
        
        Returns:
            Task对象，如果不存在返回None
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM tasks WHERE task_id = ?', (task_id,))
            row = cursor.fetchone()
            
            if row is None:
                return None
            
            columns = [desc[0] for desc in cursor.description]
            data = dict(zip(columns, row))
            return Task.from_dict(data)

    async def cancel_task(self, task_id: str) -> bool:
        """
        取消任务
        
        Args:
            task_id: 任务ID
        
        Returns:
            是否取消成功
        """
        async with self._lock:
            task = self.get_task(task_id)
            if task is None:
                return False
            
            if task.status not in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
                return False
            
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'UPDATE tasks SET status = ? WHERE task_id = ?',
                    (TaskStatus.CANCELLED.value, task_id)
                )
                conn.commit()
            
            return True

    def get_result(self, task_id: str) -> Optional[Any]:
        """
        获取任务结果
        
        Args:
            task_id: 任务ID
        
        Returns:
            任务结果，如果任务未完成返回None
        """
        task = self.get_task(task_id)
        if task is None:
            return None
        
        if task.status != TaskStatus.COMPLETED.value:
            return None
        
        return task.result

    def register_progress_callback(self, task_id: str, callback: Callable) -> None:
        """
        注册任务进度回调函数
        
        Args:
            task_id: 任务ID
            callback: 回调函数，接收 progress 参数
        """
        if task_id not in self._progress_callbacks:
            self._progress_callbacks[task_id] = []
        self._progress_callbacks[task_id].append(callback)

    def register_task_handler(self, task_name: str, handler: Callable) -> None:
        """
        注册任务处理器
        
        Args:
            task_name: 任务名称
            handler: 任务处理函数，接收 args 和 progress_callback 参数
        """
        self._task_handlers[task_name] = handler

    async def _update_progress(self, task_id: str, progress: int) -> None:
        """
        更新任务进度
        
        Args:
            task_id: 任务ID
            progress: 进度值（0-100）
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE tasks SET progress = ? WHERE task_id = ?',
                (progress, task_id)
            )
            conn.commit()
        
        # 触发进度回调
        if task_id in self._progress_callbacks:
            for callback in self._progress_callbacks[task_id]:
                callback(progress)

    async def _execute_task(self, task: Task) -> None:
        """
        执行单个任务
        
        Args:
            task: 任务对象
        """
        # 更新任务状态为运行中
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE tasks SET status = ?, started_at = ? WHERE task_id = ?',
                (TaskStatus.RUNNING.value, time.time(), task.task_id)
            )
            conn.commit()

        try:
            # 获取任务参数
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT args FROM tasks WHERE task_id = ?', (task.task_id,))
                args = json.loads(cursor.fetchone()[0])

            # 创建进度回调函数
            async def progress_callback(progress: int):
                await self._update_progress(task.task_id, progress)

            # 查找任务处理器
            handler = self._task_handlers.get(task.name)
            if handler:
                # 如果是异步处理器
                if asyncio.iscoroutinefunction(handler):
                    result = await asyncio.wait_for(
                        handler(args, progress_callback),
                        timeout=self._timeout
                    )
                else:
                    # 同步处理器包装成异步执行
                    result = await asyncio.wait_for(
                        asyncio.to_thread(handler, args, progress_callback),
                        timeout=self._timeout
                    )
            else:
                # 默认处理器：模拟耗时操作
                for i in range(10):
                    await asyncio.sleep(0.1)
                    await progress_callback((i + 1) * 10)
                result = {'status': 'completed', 'args': args}

            # 更新任务状态为完成
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE tasks SET status = ?, progress = ?, result = ?, 
                                    completed_at = ? WHERE task_id = ?
                ''', (
                    TaskStatus.COMPLETED.value,
                    100,
                    json.dumps(result),
                    time.time(),
                    task.task_id
                ))
                conn.commit()

        except asyncio.TimeoutError:
            await self._handle_task_failure(task.task_id, 'Task timeout')
        except Exception as e:
            await self._handle_task_failure(task.task_id, str(e))

    async def _handle_task_failure(self, task_id: str, error: str) -> None:
        """
        处理任务失败
        
        Args:
            task_id: 任务ID
            error: 错误信息
        """
        async with self._lock:
            task = self.get_task(task_id)
            if task is None:
                return

            task.retry_count += 1

            if task.retry_count < task.max_retries:
                # 重试任务：重置状态为pending
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE tasks SET status = ?, retry_count = ?, error = ? 
                        WHERE task_id = ?
                    ''', (TaskStatus.PENDING.value, task.retry_count, error, task_id))
                    conn.commit()
            else:
                # 超过最大重试次数，标记为失败
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE tasks SET status = ?, progress = ?, error = ?,
                                        completed_at = ? WHERE task_id = ?
                    ''', (
                        TaskStatus.FAILED.value,
                        0,
                        error,
                        time.time(),
                        task_id
                    ))
                    conn.commit()

    async def _worker(self) -> None:
        """工作协程，循环处理任务"""
        while self._running:
            async with self._lock:
                # 获取优先级最高的pending任务
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT * FROM tasks WHERE status = ? 
                        ORDER BY priority_weight, created_at LIMIT 1
                    ''', (TaskStatus.PENDING.value,))
                    row = cursor.fetchone()

                    if row:
                        columns = [desc[0] for desc in cursor.description]
                        data = dict(zip(columns, row))
                        task = Task.from_dict(data)
                        
                        # 立即更新状态为running，防止其他worker重复处理
                        cursor.execute(
                            'UPDATE tasks SET status = ? WHERE task_id = ?',
                            (TaskStatus.RUNNING.value, task.task_id)
                        )
                        conn.commit()
                    else:
                        task = None

            if task:
                await self._execute_task(task)
            else:
                await asyncio.sleep(0.1)

    async def start(self, num_workers: int = 3) -> None:
        """
        启动任务队列
        
        Args:
            num_workers: 工作协程数量
        """
        # 确保数据库有priority_weight列用于排序
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(tasks)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'priority_weight' not in columns:
                cursor.execute('ALTER TABLE tasks ADD COLUMN priority_weight INTEGER DEFAULT 1')
                conn.commit()
                # 更新现有记录的权重
                cursor.execute("UPDATE tasks SET priority_weight = CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END")
                conn.commit()

        self._running = True
        for _ in range(num_workers):
            worker = asyncio.create_task(self._worker())
            self._workers.append(worker)

    async def stop(self) -> None:
        """停止任务队列"""
        self._running = False
        await asyncio.gather(*self._workers)
        self._workers.clear()

    def list_tasks(self, status: Optional[str] = None) -> List[Task]:
        """
        列出任务
        
        Args:
            status: 过滤状态，如果为None则返回所有任务
        
        Returns:
            任务列表
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute('SELECT * FROM tasks WHERE status = ?', (status,))
            else:
                cursor.execute('SELECT * FROM tasks')
            
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            tasks = []
            for row in rows:
                data = dict(zip(columns, row))
                tasks.append(Task.from_dict(data))
            
            return tasks

    def set_timeout(self, timeout: int) -> None:
        """
        设置任务超时时间
        
        Args:
            timeout: 超时时间（秒）
        """
        self._timeout = timeout
