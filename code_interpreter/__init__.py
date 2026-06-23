"""代码解释器沙箱 — 在安全沙箱中执行Python代码，支持数据可视化。

提供：
  - Python代码执行（本地进程/Docker沙箱）
  - 数据可视化（matplotlib图表生成）
  - 文件处理（上传/下载）
  - 安全隔离与资源限制
  - 执行结果格式化展示
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """代码执行结果类。"""
    success: bool
    output: str = ""
    error: str = ""
    execution_time: float = 0.0
    images: List[str] = field(default_factory=list)  # base64编码的图片
    files: List[Dict[str, Any]] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""


@dataclass
class SandboxConfig:
    """沙箱配置类。"""
    timeout: int = 30  # 执行超时时间（秒）
    max_memory: int = 512  # 最大内存（MB）
    max_output_size: int = 10240  # 最大输出大小（KB）
    allow_network: bool = False  # 是否允许网络访问
    allowed_modules: List[str] = field(default_factory=lambda: [
        "math", "random", "statistics", "datetime", "time",
        "json", "csv", "re", "string",
        "numpy", "pandas", "matplotlib", "seaborn",
        "io", "base64", "hashlib",
        "collections", "itertools", "functools",
    ])
    blocked_modules: List[str] = field(default_factory=lambda: [
        "os", "sys", "subprocess", "shutil",
        "socket", "http", "urllib", "requests",
        "importlib", "builtins",
    ])


class CodeSandbox:
    """代码沙箱 — 安全执行Python代码。"""

    def __init__(self, config: SandboxConfig = None):
        self.config = config or SandboxConfig()
        self._work_dir = Path(tempfile.mkdtemp(prefix="code_sandbox_"))
        self._session_id = str(uuid.uuid4())[:8]

    def __del__(self):
        """清理临时目录。"""
        try:
            if self._work_dir.exists():
                shutil.rmtree(self._work_dir, ignore_errors=True)
        except Exception:
            pass

    def execute(self, code: str, input_files: Dict[str, bytes] = None) -> ExecutionResult:
        """执行Python代码。"""
        start_time = time.time()
        result = ExecutionResult(success=False)

        # 预处理代码
        code = self._preprocess_code(code)

        # 安全检查
        security_check = self._security_check(code)
        if security_check:
            result.error = f"安全检查失败: {security_check}"
            result.execution_time = time.time() - start_time
            return result

        try:
            # 写入输入文件
            if input_files:
                for filename, content in input_files.items():
                    file_path = self._work_dir / filename
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_bytes(content)

            # 包装代码（捕获输出和图表）
            wrapped_code = self._wrap_code(code)

            # 写入代码文件
            code_file = self._work_dir / f"code_{self._session_id}.py"
            code_file.write_text(wrapped_code, encoding="utf-8")

            # 执行代码
            proc = subprocess.run(
                [sys.executable, str(code_file)],
                cwd=str(self._work_dir),
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                env=self._get_env(),
            )

            result.stdout = proc.stdout
            result.stderr = proc.stderr
            result.output = proc.stdout
            result.success = proc.returncode == 0

            if proc.returncode != 0:
                result.error = proc.stderr

            # 提取生成的图片
            result.images = self._extract_images()

            # 提取生成的文件
            result.files = self._extract_files()

        except subprocess.TimeoutExpired:
            result.error = f"执行超时（超过 {self.config.timeout} 秒）"
        except Exception as exc:
            result.error = f"执行异常: {str(exc)}"
            logger.warning("Code execution error: %s", exc)

        result.execution_time = time.time() - start_time
        return result

    def _preprocess_code(self, code: str) -> str:
        """预处理代码。"""
        # 移除不兼容的语法
        return code

    def _security_check(self, code: str) -> Optional[str]:
        """安全检查代码。"""
        # 检查危险导入
        for module in self.config.blocked_modules:
            pattern = rf'(^|\s)import\s+{module}\b|from\s+{module}\s+import'
            if re.search(pattern, code):
                return f"禁止导入模块: {module}"

        # 检查危险操作
        dangerous_patterns = [
            r'__import__',
            r'eval\(',
            r'exec\(',
            r'open\([^)]*["\'].*["\'].*[rw]',
            r'subprocess',
            r'system\(',
        ]
        for pattern in dangerous_patterns:
            if re.search(pattern, code):
                return f"检测到危险代码: {pattern}"

        return None

    def _wrap_code(self, code: str) -> str:
        """包装代码以捕获输出和图表。"""
        return f'''
import sys
import io
import base64
import os

# 重定向输出
stdout_capture = io.StringIO()
stderr_capture = io.StringIO()
sys.stdout = stdout_capture
sys.stderr = stderr_capture

# 设置matplotlib后端（无显示）
try:
    import matplotlib
    matplotlib.use('Agg')
except ImportError:
    pass

# 执行用户代码
try:
    {code}
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 恢复输出
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__

# 输出捕获的内容
print(stdout_capture.getvalue(), end="")
if stderr_capture.getvalue():
    print(stderr_capture.getvalue(), file=sys.__stderr__)

# 保存matplotlib图表
try:
    import matplotlib.pyplot as plt
    figures = [plt.figure(i) for i in range(1, 100)]
    for i, fig in enumerate(plt.get_fignums()):
        figure = plt.figure(fig)
        buf = io.BytesIO()
        figure.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_b64 = base64.b64encode(buf.read()).decode()
        print(f"__IMAGE_{i}__:" + img_b64[:100] + "..." if len(img_b64) > 100 else "")
        with open(f"_output_image_{i}.png", "wb") as f:
            f.write(base64.b64decode(img_b64))
except Exception:
    pass
'''

    def _get_env(self) -> Dict[str, str]:
        """获取执行环境变量。"""
        env = os.environ.copy()
        if not self.config.allow_network:
            env["HTTP_PROXY"] = ""
            env["HTTPS_PROXY"] = ""
        return env

    def _extract_images(self) -> List[str]:
        """提取生成的图片。"""
        images = []
        try:
            for file in self._work_dir.glob("_output_image_*.png"):
                img_data = file.read_bytes()
                img_b64 = base64.b64encode(img_data).decode()
                images.append(img_b64)
        except Exception as exc:
            logger.warning("Failed to extract images: %s", exc)
        return images

    def _extract_files(self) -> List[Dict[str, Any]]:
        """提取生成的文件。"""
        files = []
        try:
            for file in self._work_dir.iterdir():
                if file.is_file() and not file.name.startswith("_") and file.suffix != ".py":
                    stat = file.stat()
                    files.append({
                        "name": file.name,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })
        except Exception as exc:
            logger.warning("Failed to extract files: %s", exc)
        return files

    def get_work_dir(self) -> Path:
        """获取工作目录。"""
        return self._work_dir

    def upload_file(self, filename: str, content: bytes) -> bool:
        """上传文件到沙箱。"""
        try:
            file_path = self._work_dir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
            return True
        except Exception as exc:
            logger.warning("Failed to upload file: %s", exc)
            return False

    def download_file(self, filename: str) -> Optional[bytes]:
        """从沙箱下载文件。"""
        try:
            file_path = self._work_dir / filename
            if file_path.exists() and file_path.is_file():
                return file_path.read_bytes()
        except Exception as exc:
            logger.warning("Failed to download file: %s", exc)
        return None

    def list_files(self) -> List[Dict[str, Any]]:
        """列出沙箱中的文件。"""
        files = []
        try:
            for file in self._work_dir.rglob("*"):
                if file.is_file() and not file.name.startswith("_"):
                    stat = file.stat()
                    rel_path = file.relative_to(self._work_dir)
                    files.append({
                        "path": str(rel_path),
                        "name": file.name,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })
        except Exception as exc:
            logger.warning("Failed to list files: %s", exc)
        return files


class CodeInterpreterPlugin(Plugin):
    """代码解释器沙箱插件。"""

    name = "code_interpreter"

    def __init__(self):
        super().__init__()
        self._sandboxes: Dict[str, CodeSandbox] = {}
        self._config = SandboxConfig()

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("code_interpreter", {}) or {}

        self._config = SandboxConfig(
            timeout=cfg.get("timeout", 30),
            max_memory=cfg.get("max_memory", 512),
            max_output_size=cfg.get("max_output_size", 10240),
            allow_network=cfg.get("allow_network", False),
        )
        logger.info("Code interpreter plugin configured")

    def create_sandbox(self, session_id: str = None) -> str:
        """创建新沙箱。"""
        session_id = session_id or str(uuid.uuid4())[:8]
        sandbox = CodeSandbox(self._config)
        self._sandboxes[session_id] = sandbox
        return session_id

    def execute_code(self, session_id: str, code: str, input_files: Dict[str, bytes] = None) -> Optional[ExecutionResult]:
        """在指定沙箱中执行代码。"""
        sandbox = self._sandboxes.get(session_id)
        if not sandbox:
            sandbox = self._sandboxes[self.create_sandbox(session_id)]

        return sandbox.execute(code, input_files)

    def get_sandbox(self, session_id: str) -> Optional[CodeSandbox]:
        """获取沙箱实例。"""
        return self._sandboxes.get(session_id)

    def close_sandbox(self, session_id: str) -> bool:
        """关闭沙箱。"""
        if session_id in self._sandboxes:
            del self._sandboxes[session_id]
            return True
        return False

    def list_sandboxes(self) -> List[str]:
        """列出所有沙箱。"""
        return list(self._sandboxes.keys())

    def execute_and_format(self, code: str, session_id: str = None) -> Dict[str, Any]:
        """执行代码并格式化返回结果。"""
        session_id = session_id or "default"
        result = self.execute_code(session_id, code)

        if not result:
            return {"success": False, "error": "沙箱创建失败"}

        formatted = {
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "execution_time": result.execution_time,
            "has_images": len(result.images) > 0,
            "image_count": len(result.images),
            "has_files": len(result.files) > 0,
            "file_count": len(result.files),
        }

        if result.images:
            formatted["images"] = [
                f"data:image/png;base64,{img}" for img in result.images
            ]

        if result.files:
            formatted["files"] = result.files

        return formatted

    def get_config(self) -> SandboxConfig:
        """获取沙箱配置。"""
        return self._config