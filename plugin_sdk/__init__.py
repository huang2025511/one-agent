"""插件开发SDK — 插件脚手架生成、调试工具、测试框架。

提供：
  - 插件脚手架生成
  - 本地调试工具
  - 插件测试框架
  - 文档自动生成
  - 插件发布向导
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class PluginTemplate:
    """插件模板类。"""
    template_id: str
    name: str
    description: str
    category: str
    files: Dict[str, str] = field(default_factory=dict)  # 文件名 -> 内容模板
    dependencies: List[str] = field(default_factory=list)


@dataclass
class PluginInfo:
    """插件信息类。"""
    name: str
    version: str
    description: str
    author: str
    entry_point: str
    dependencies: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    license: str = "MIT"
    homepage: str = ""


class PluginScaffolder:
    """插件脚手架生成器。"""

    TEMPLATES = {
        "basic": PluginTemplate(
            template_id="basic",
            name="基础插件",
            description="最基础的插件模板",
            category="general",
            files={
                "__init__.py": '''"""{{plugin_name}} plugin — {{description}}."""

from __future__ import annotations

import logging
from core.plugin import Plugin

logger = logging.getLogger(__name__)


class {{class_name}}Plugin(Plugin):
    """{{plugin_name}} Plugin."""

    name = "{{plugin_id}}"

    def __init__(self):
        super().__init__()

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        logger.info("{{plugin_name}} plugin configured")
''',
                "README.md": '''# {{plugin_name}}

{{description}}

## Installation

```bash
pip install {{plugin_id}}
```

## Usage

```python
from {{plugin_id}} import {{class_name}}Plugin
```
''',
                "requirements.txt": '''# {{plugin_name}} dependencies
''',
            },
        ),
        "skill": PluginTemplate(
            template_id="skill",
            name="技能插件",
            description="技能类型插件模板",
            category="skills",
            files={
                "__init__.py": '''"""{{plugin_name}} skill plugin — {{description}}."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from core.plugin import Plugin

logger = logging.getLogger(__name__)


class {{class_name}}SkillPlugin(Plugin):
    """{{plugin_name}} Skill Plugin."""

    name = "{{plugin_id}}"
    skill_type = "custom"

    def __init__(self):
        super().__init__()
        self._tools = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._register_tools()
        logger.info("{{plugin_name}} skill plugin configured")

    def _register_tools(self):
        """注册工具函数。"""
        pass

    def get_tools(self) -> List[Dict[str, Any]]:
        """获取可用工具列表。"""
        return list(self._tools.values())

    async def execute_tool(self, tool_name: str, parameters: Dict[str, Any]) -> Any:
        """执行工具。"""
        if tool_name not in self._tools:
            raise ValueError(f"Tool not found: {tool_name}")
        return self._tools[tool_name](**parameters)
''',
                "README.md": '''# {{plugin_name}}

{{description}}

## Skills

- skill_1: Description of skill 1
- skill_2: Description of skill 2
''',
            },
        ),
        "api_extension": PluginTemplate(
            template_id="api_extension",
            name="API扩展插件",
            description="扩展REST API的插件模板",
            category="api",
            files={
                "__init__.py": '''"""{{plugin_name}} API extension — {{description}}."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from core.plugin import Plugin

logger = logging.getLogger(__name__)


class {{class_name}}APIPlugin(Plugin):
    """{{plugin_name}} API Extension Plugin."""

    name = "{{plugin_id}}"

    def __init__(self):
        super().__init__()
        self._routes = []

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._register_routes()
        logger.info("{{plugin_name}} API plugin configured")

    def _register_routes(self):
        """注册API路由。"""
        pass

    def get_routes(self) -> List[Dict[str, Any]]:
        """获取所有路由。"""
        return self._routes
''',
                "README.md": '''# {{plugin_name}}

{{description}}

## API Endpoints

- GET /api/{{plugin_id}}/status - Get plugin status
- POST /api/{{plugin_id}}/action - Execute action
''',
            },
        ),
        "gateway": PluginTemplate(
            template_id="gateway",
            name="网关插件",
            description="消息网关类型插件模板",
            category="gateway",
            files={
                "__init__.py": '''"""{{plugin_name}} gateway — {{description}}."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from core.plugin import Plugin

logger = logging.getLogger(__name__)


class {{class_name}}GatewayPlugin(Plugin):
    """{{plugin_name}} Gateway Plugin."""

    name = "{{plugin_id}}"
    gateway_type = "custom"

    def __init__(self):
        super().__init__()
        self._running = False

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        logger.info("{{plugin_name}} gateway plugin configured")

    async def start(self) -> None:
        """启动网关。"""
        self._running = True
        logger.info("{{plugin_name}} gateway started")

    async def stop(self) -> None:
        """停止网关。"""
        self._running = False
        logger.info("{{plugin_name}} gateway stopped")

    async def send_message(self, user_id: str, message: str) -> bool:
        """发送消息。"""
        return True

    def is_running(self) -> bool:
        """检查是否运行中。"""
        return self._running
''',
                "README.md": '''# {{plugin_name}}

{{description}}

## Setup

Configure the gateway in your config file.
''',
            },
        ),
    }

    def __init__(self, output_dir: str = "plugins"):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def list_templates(self) -> List[Dict[str, Any]]:
        """列出可用模板。"""
        return [
            {
                "template_id": t.template_id,
                "name": t.name,
                "description": t.description,
                "category": t.category,
            }
            for t in self.TEMPLATES.values()
        ]

    def generate(self, template_id: str, plugin_id: str, plugin_name: str,
                 description: str = "", author: str = "",
                 output_dir: str = None) -> Optional[str]:
        """生成插件脚手架。"""
        template = self.TEMPLATES.get(template_id)
        if not template:
            logger.error("Template not found: %s", template_id)
            return None

        # 生成类名
        class_name = "".join(word.capitalize() for word in plugin_id.replace("_", " ").split())

        # 输出目录
        target_dir = Path(output_dir) if output_dir else self._output_dir / plugin_id
        target_dir.mkdir(parents=True, exist_ok=True)

        # 渲染文件
        for filename, content_template in template.files.items():
            content = content_template
            content = content.replace("{{plugin_id}}", plugin_id)
            content = content.replace("{{plugin_name}}", plugin_name)
            content = content.replace("{{class_name}}", class_name)
            content = content.replace("{{description}}", description)
            content = content.replace("{{author}}", author)

            file_path = target_dir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        # 生成plugin.json元数据
        plugin_info = {
            "name": plugin_name,
            "id": plugin_id,
            "version": "0.1.0",
            "description": description,
            "author": author,
            "entry_point": f"{plugin_id}.{class_name}Plugin",
            "dependencies": template.dependencies,
            "type": template.category,
        }
        (target_dir / "plugin.json").write_text(
            json.dumps(plugin_info, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        logger.info("Plugin scaffold generated: %s", target_dir)
        return str(target_dir)


class PluginTester:
    """插件测试框架。"""

    def __init__(self):
        self._test_results: Dict[str, Dict[str, Any]] = {}

    def load_plugin(self, plugin_path: str) -> Optional[Plugin]:
        """加载插件。"""
        import importlib.util
        import sys

        plugin_dir = Path(plugin_path)
        init_file = plugin_dir / "__init__.py"

        if not init_file.exists():
            logger.error("Plugin init file not found: %s", init_file)
            return None

        plugin_name = plugin_dir.name
        spec = importlib.util.spec_from_file_location(plugin_name, str(init_file))
        if spec is None or spec.loader is None:
            return None

        module = importlib.util.module_from_spec(spec)
        sys.modules[plugin_name] = module
        spec.loader.exec_module(module)

        # 查找Plugin子类
        from core.plugin import Plugin
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
                return attr()

        return None

    def run_basic_tests(self, plugin_path: str) -> Dict[str, Any]:
        """运行基础测试。"""
        results = {
            "plugin_path": plugin_path,
            "load": {"passed": False, "error": ""},
            "setup": {"passed": False, "error": ""},
            "name": {"passed": False, "error": ""},
        }

        # 加载测试
        plugin = self.load_plugin(plugin_path)
        if plugin:
            results["load"]["passed"] = True
            results["plugin_name"] = plugin.name
        else:
            results["load"]["error"] = "Failed to load plugin"
            return results

        # 名称测试
        if plugin.name:
            results["name"]["passed"] = True
        else:
            results["name"]["error"] = "Plugin name is empty"

        return results

    def validate_metadata(self, plugin_path: str) -> Dict[str, Any]:
        """验证插件元数据。"""
        results = {
            "has_plugin_json": False,
            "has_readme": False,
            "has_requirements": False,
            "metadata": {},
            "errors": [],
        }

        plugin_dir = Path(plugin_path)

        # 检查文件
        plugin_json = plugin_dir / "plugin.json"
        if plugin_json.exists():
            results["has_plugin_json"] = True
            try:
                with open(plugin_json, "r", encoding="utf-8") as f:
                    results["metadata"] = json.load(f)
            except Exception as exc:
                results["errors"].append(f"Invalid plugin.json: {exc}")

        readme = plugin_dir / "README.md"
        if readme.exists():
            results["has_readme"] = True

        requirements = plugin_dir / "requirements.txt"
        if requirements.exists():
            results["has_requirements"] = True

        return results


class PluginDocGenerator:
    """插件文档自动生成器。"""

    def generate_readme(self, plugin_info: Dict[str, Any]) -> str:
        """生成README文档。"""
        name = plugin_info.get("name", "Plugin")
        description = plugin_info.get("description", "")
        version = plugin_info.get("version", "0.1.0")
        author = plugin_info.get("author", "")
        dependencies = plugin_info.get("dependencies", [])

        readme = f"""# {name}

> {description}

## Installation

```bash
pip install {plugin_info.get('id', name)}
```

## Features

- Feature 1
- Feature 2
- Feature 3

## Usage

```python
from {plugin_info.get('id', name)} import Plugin
```

## Configuration

```yaml
plugins:
  {plugin_info.get('id', name)}:
    enabled: true
    option: value
```

## Dependencies

"""
        if dependencies:
            for dep in dependencies:
                readme += f"- {dep}\n"
        else:
            readme += "None\n"

        readme += f"""
## Version

- Current version: {version}
- Author: {author}
- License: {plugin_info.get('license', 'MIT')}
"""
        return readme

    def generate_api_docs(self, plugin) -> str:
        """生成API文档。"""
        import inspect

        docs = f"# {plugin.__class__.__name__} API\n\n"
        docs += f"> {plugin.__doc__ or ''}\n\n"

        # 获取公共方法
        methods = [
            method for method in dir(plugin)
            if not method.startswith("_") and callable(getattr(plugin, method))
        ]

        for method_name in methods:
            method = getattr(plugin, method_name)
            try:
                sig = inspect.signature(method)
                docs += f"## {method_name}\n\n"
                docs += f"```python\n{method_name}{sig}\n```\n\n"
                if method.__doc__:
                    docs += f"{method.__doc__}\n\n"
            except (TypeError, ValueError):
                pass

        return docs


class PluginPublisher:
    """插件发布向导。"""

    def __init__(self):
        self._publish_dir = Path("data/plugins/dist")
        self._publish_dir.mkdir(parents=True, exist_ok=True)

    def validate_plugin(self, plugin_path: str) -> Dict[str, Any]:
        """验证插件是否可以发布。"""
        results = {
            "valid": True,
            "checks": [],
            "errors": [],
            "warnings": [],
        }

        plugin_dir = Path(plugin_path)

        # 检查必需文件
        required_files = ["__init__.py", "plugin.json", "README.md"]
        for file in required_files:
            if (plugin_dir / file).exists():
                results["checks"].append({"file": file, "passed": True})
            else:
                results["errors"].append(f"Missing required file: {file}")
                results["valid"] = False

        # 读取元数据
        plugin_json = plugin_dir / "plugin.json"
        if plugin_json.exists():
            try:
                with open(plugin_json, "r", encoding="utf-8") as f:
                    metadata = json.load(f)

                # 验证元数据
                required_fields = ["name", "id", "version", "entry_point"]
                for field in required_fields:
                    if not metadata.get(field):
                        results["errors"].append(f"Missing required field: {field}")
                        results["valid"] = False
            except Exception as exc:
                results["errors"].append(f"Invalid plugin.json: {exc}")
                results["valid"] = False

        return results

    def package_plugin(self, plugin_path: str, output_path: str = None) -> Optional[str]:
        """打包插件。"""
        plugin_dir = Path(plugin_path)
        if not plugin_dir.exists():
            logger.error("Plugin path not found: %s", plugin_path)
            return None

        # 读取元数据
        plugin_json = plugin_dir / "plugin.json"
        if plugin_json.exists():
            with open(plugin_json, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            plugin_id = metadata.get("id", plugin_dir.name)
            version = metadata.get("version", "0.1.0")
        else:
            plugin_id = plugin_dir.name
            version = "0.1.0"

        # 输出路径
        if output_path is None:
            output_path = str(self._publish_dir / f"{plugin_id}-{version}.zip")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # 打包
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(plugin_dir):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(plugin_dir.parent)
                    zipf.write(file_path, arcname)

        logger.info("Plugin packaged: %s", output_path)
        return output_path

    def generate_manifest(self, plugin_path: str) -> Dict[str, Any]:
        """生成插件清单。"""
        plugin_dir = Path(plugin_path)
        plugin_json = plugin_dir / "plugin.json"

        if plugin_json.exists():
            with open(plugin_json, "r", encoding="utf-8") as f:
                metadata = json.load(f)
        else:
            metadata = {"id": plugin_dir.name, "name": plugin_dir.name}

        # 计算文件列表和大小
        files = []
        total_size = 0
        for root, dirs, file_names in os.walk(plugin_dir):
            for file_name in file_names:
                file_path = Path(root) / file_name
                size = file_path.stat().st_size
                total_size += size
                files.append({
                    "path": str(file_path.relative_to(plugin_dir)),
                    "size": size,
                })

        return {
            **metadata,
            "file_count": len(files),
            "total_size": total_size,
            "files": files,
            "published_at": time.time(),
        }


class PluginSDKPlugin(Plugin):
    """插件开发SDK插件。"""

    name = "plugin_sdk"

    def __init__(self):
        super().__init__()
        self._scaffolder = PluginScaffolder()
        self._tester = PluginTester()
        self._doc_generator = PluginDocGenerator()
        self._publisher = PluginPublisher()

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        logger.info("Plugin SDK plugin configured")

    def list_templates(self) -> List[Dict[str, Any]]:
        """列出可用模板。"""
        return self._scaffolder.list_templates()

    def generate_plugin(self, template_id: str, plugin_id: str, plugin_name: str,
                        description: str = "", author: str = "") -> Optional[str]:
        """生成插件脚手架。"""
        return self._scaffolder.generate(template_id, plugin_id, plugin_name, description, author)

    def test_plugin(self, plugin_path: str) -> Dict[str, Any]:
        """测试插件。"""
        return self._tester.run_basic_tests(plugin_path)

    def validate_plugin(self, plugin_path: str) -> Dict[str, Any]:
        """验证插件元数据。"""
        return self._tester.validate_metadata(plugin_path)

    def generate_docs(self, plugin_path: str) -> Optional[str]:
        """生成插件文档。"""
        plugin = self._tester.load_plugin(plugin_path)
        if plugin:
            return self._doc_generator.generate_api_docs(plugin)
        return None

    def package_plugin(self, plugin_path: str) -> Optional[str]:
        """打包插件。"""
        return self._publisher.package_plugin(plugin_path)

    def get_scaffolder(self) -> PluginScaffolder:
        """获取脚手架生成器。"""
        return self._scaffolder

    def get_tester(self) -> PluginTester:
        """获取测试框架。"""
        return self._tester

    def get_doc_generator(self) -> PluginDocGenerator:
        """获取文档生成器。"""
        return self._doc_generator

    def get_publisher(self) -> PluginPublisher:
        """获取发布器。"""
        return self._publisher