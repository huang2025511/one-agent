"""模块自动加载器 — 为可选模块注册代理技能，实现按需自动加载。

工作原理：
  1. 启动时为每个可选模块注册一个"代理技能"到 SkillManager
  2. 代理技能的 title/description 包含丰富的关键词，确保 pick_relevant 能命中
  3. 当 LLM 调用某个代理技能时，handler 先通过 ModuleRegistry 加载模块
  4. 模块加载后，将实际功能委托给模块的方法

这样 LLM 始终能"看到"所有模块的能力描述，但模块本身只在被调用时才加载。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from core.context import AgentContext
    from core.module_registry import ModuleRegistry
    from skills import Skill, SkillManager

logger = logging.getLogger(__name__)


# ======================================================================
# 模块能力描述 — 每个可选模块的代理技能定义
# ======================================================================

MODULE_SKILLS: List[Dict[str, Any]] = [
    # --- 基础增强 ---
    {
        "module": "multimodal",
        "skill_id": "image_generate",
        "title": "图像生成",
        "description": "AI图像生成 图片生成 画图 生成图片 DALL-E 绘画 作图 multimodal 多模态 生成插画",
        "schema": {
            "type": "function",
            "function": {
                "name": "image_generate",
                "description": "使用AI生成图像。支持DALL-E、Stability AI等模型。输入文字描述即可生成图片。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "图像描述（如：一只在月光下的猫）"},
                        "size": {"type": "string", "description": "图片尺寸：square_hd/square/portrait_4_3/landscape_16_9", "default": "square"},
                        "model": {"type": "string", "description": "模型名称（可选）", "default": ""},
                    },
                    "required": ["prompt"],
                },
            },
        },
    },
    {
        "module": "multimodal",
        "skill_id": "image_describe",
        "title": "图像理解",
        "description": "图像理解 图片识别 看图 图片描述 分析图片 识别图像 multimodal 多模态 vision",
        "schema": {
            "type": "function",
            "function": {
                "name": "image_describe",
                "description": "分析并描述图片内容。支持GPT-4V、Claude Vision等视觉模型。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image_url": {"type": "string", "description": "图片URL或base64编码"},
                        "question": {"type": "string", "description": "关于图片的问题（可选）", "default": ""},
                    },
                    "required": ["image_url"],
                },
            },
        },
    },
    {
        "module": "multimodal",
        "skill_id": "text_to_speech",
        "title": "文本转语音",
        "description": "文本转语音 语音合成 TTS 朗读 语音生成 读出来 multimodal 多模态 语音 音频",
        "schema": {
            "type": "function",
            "function": {
                "name": "text_to_speech",
                "description": "将文本转换为语音音频。支持OpenAI TTS、ElevenLabs等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要转换的文本"},
                        "voice": {"type": "string", "description": "语音类型（可选）", "default": ""},
                    },
                    "required": ["text"],
                },
            },
        },
    },
    {
        "module": "monitor",
        "skill_id": "system_monitor",
        "title": "系统监控",
        "description": "系统监控 性能指标 运行状态 系统状态 monitor 监控 指标 metrics 健康检查",
        "schema": {
            "type": "function",
            "function": {
                "name": "system_monitor",
                "description": "获取系统运行指标，包括事件总线、LLM调用、内存使用、技能调用等统计数据。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：metrics（获取指标）/ health（健康检查）", "default": "metrics"},
                    },
                },
            },
        },
    },
    {
        "module": "marketplace",
        "skill_id": "skill_marketplace",
        "title": "技能市场",
        "description": "技能市场 安装技能 技能商店 marketplace 搜索技能 安装插件 社区技能 技能包",
        "schema": {
            "type": "function",
            "function": {
                "name": "skill_marketplace",
                "description": "浏览、搜索、安装和管理技能包。从技能市场获取社区开发的技能。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：search/list/install/uninstall/rate", "default": "list"},
                        "query": {"type": "string", "description": "搜索关键词（action=search时使用）", "default": ""},
                        "skill_id": {"type": "string", "description": "技能ID（action=install/uninstall/rate时使用）", "default": ""},
                        "rating": {"type": "number", "description": "评分1-5（action=rate时使用）", "default": 5},
                    },
                },
            },
        },
    },
    {
        "module": "alerting",
        "skill_id": "alert_manage",
        "title": "告警管理",
        "description": "告警 报警 告警规则 告警通知 alerting 预警 监控告警 告警历史",
        "schema": {
            "type": "function",
            "function": {
                "name": "alert_manage",
                "description": "管理告警规则和查看告警历史。设置指标阈值，当系统指标超过阈值时触发告警通知。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：list_rules/add_rule/remove/list_alerts", "default": "list_rules"},
                        "rule_name": {"type": "string", "description": "规则名称", "default": ""},
                        "metric": {"type": "string", "description": "监控指标名", "default": ""},
                        "threshold": {"type": "number", "description": "阈值", "default": 0},
                    },
                },
            },
        },
    },
    {
        "module": "rest_api",
        "skill_id": "api_status",
        "title": "API服务",
        "description": "REST API API服务 接口 api_status rest_api 远程调用 HTTP接口",
        "schema": {
            "type": "function",
            "function": {
                "name": "api_status",
                "description": "查看REST API服务状态和可用端点。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        },
    },
    # --- AI 能力扩展 ---
    {
        "module": "rag",
        "skill_id": "knowledge_search",
        "title": "知识库搜索",
        "description": "知识库 RAG 文档搜索 检索增强 知识检索 文档问答 knowledge_search rag 知识库查询 向量检索",
        "schema": {
            "type": "function",
            "function": {
                "name": "knowledge_search",
                "description": "在知识库中搜索相关信息。支持上传文档构建知识库，使用RAG技术进行检索增强问答。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索查询"},
                        "top_k": {"type": "integer", "description": "返回结果数量", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        },
    },
    {
        "module": "automl",
        "skill_id": "auto_ml",
        "title": "自动机器学习",
        "description": "机器学习 自动训练 模型训练 AutoML 数据分析 特征工程 automl 自动机器学习 模型选择 训练模型",
        "schema": {
            "type": "function",
            "function": {
                "name": "auto_ml",
                "description": "自动化机器学习流程：自动特征工程、模型选择、超参数优化和模型评估。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：train/evaluate/list_models", "default": "train"},
                        "data": {"type": "string", "description": "训练数据（CSV格式或JSON）", "default": ""},
                        "target": {"type": "string", "description": "目标列名", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "active_learning",
        "skill_id": "self_evolve",
        "title": "自我进化",
        "description": "自我进化 主动学习 持续学习 知识更新 自我改进 active_learning 学习能力 知识积累",
        "schema": {
            "type": "function",
            "function": {
                "name": "self_evolve",
                "description": "查看Agent的学习进度、已掌握的知识、识别的知识盲区和学习计划。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：progress/knowledge/gaps/plan", "default": "progress"},
                    },
                },
            },
        },
    },
    {
        "module": "document_intelligence",
        "skill_id": "doc_analyze",
        "title": "文档智能分析",
        "description": "文档分析 表单识别 票据识别 合同审查 文档问答 表格识别 版面分析 document_intelligence 文档智能",
        "schema": {
            "type": "function",
            "function": {
                "name": "doc_analyze",
                "description": "智能处理文档：表单字段提取、票据信息识别、合同条款审查、文档问答、表格识别和版面分析。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作类型：form/receipt/contract/vqa/table/layout", "default": "vqa"},
                        "text": {"type": "string", "description": "文档文本内容"},
                        "question": {"type": "string", "description": "问题（action=vqa时使用）", "default": ""},
                    },
                    "required": ["text"],
                },
            },
        },
    },
    {
        "module": "multi_agent",
        "skill_id": "multi_agent_collab",
        "title": "多智能体协作",
        "description": "多智能体 协作 任务分解 群体决策 多Agent multi_agent 协同工作 智能体编排",
        "schema": {
            "type": "function",
            "function": {
                "name": "multi_agent_collab",
                "description": "多智能体协作引擎：将复杂任务分解为子任务，分配给不同角色的Agent协作完成。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "要协作完成的任务描述"},
                        "mode": {"type": "string", "description": "协作模式：master_slave/peer", "default": "master_slave"},
                    },
                    "required": ["task"],
                },
            },
        },
    },
    {
        "module": "fine_tune",
        "skill_id": "model_finetune",
        "title": "模型微调",
        "description": "模型微调 LoRA QLoRA 训练 fine_tune 微调模型 参数高效微调 模型定制",
        "schema": {
            "type": "function",
            "function": {
                "name": "model_finetune",
                "description": "模型微调支持：基于LoRA/QLoRA的参数高效微调，包括数据集准备、训练进度追踪和模型导出。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：prepare/train/export/status", "default": "status"},
                        "model": {"type": "string", "description": "基础模型名称", "default": ""},
                        "dataset": {"type": "string", "description": "数据集路径", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "recommender",
        "skill_id": "recommend",
        "title": "推荐引擎",
        "description": "推荐 个性化推荐 推荐系统 协同过滤 内容推荐 recommender 推荐算法 相似物品",
        "schema": {
            "type": "function",
            "function": {
                "name": "recommend",
                "description": "个性化推荐引擎：基于协同过滤和内容相似度推荐物品。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "用户ID"},
                        "method": {"type": "string", "description": "推荐方法：cf_user/cf_item/content/cold_start", "default": "cf_user"},
                        "top_k": {"type": "integer", "description": "返回推荐数量", "default": 10},
                    },
                    "required": ["user_id"],
                },
            },
        },
    },
    {
        "module": "prompt_studio",
        "skill_id": "prompt_engineer",
        "title": "Prompt工程",
        "description": "Prompt工程 提示词优化 提示词模板 prompt_studio prompt设计 A/B测试 prompt优化",
        "schema": {
            "type": "function",
            "function": {
                "name": "prompt_engineer",
                "description": "Prompt工程工作台：模板编辑、版本控制、A/B测试和效果评估。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：list/create/test/optimize", "default": "list"},
                        "template_id": {"type": "string", "description": "模板ID", "default": ""},
                        "content": {"type": "string", "description": "Prompt内容", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "agent_templates",
        "skill_id": "agent_template",
        "title": "Agent模板",
        "description": "Agent模板 预设角色 模板创建 agent_templates 快速创建 编程助手 文案写作 数据分析",
        "schema": {
            "type": "function",
            "function": {
                "name": "agent_template",
                "description": "Agent模板市场：浏览和使用预配置的Agent模板（编程助手、文案写作、数据分析等）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：list/search/apply", "default": "list"},
                        "category": {"type": "string", "description": "分类筛选", "default": ""},
                        "template_id": {"type": "string", "description": "模板ID（action=apply时使用）", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "code_interpreter",
        "skill_id": "code_run",
        "title": "代码执行沙箱",
        "description": "代码执行 运行代码 Python沙箱 数据可视化 code_interpreter 代码解释器 执行Python 代码运行",
        "schema": {
            "type": "function",
            "function": {
                "name": "code_run",
                "description": "在安全沙箱中执行Python代码，支持数据可视化和文件处理。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "要执行的Python代码"},
                        "timeout": {"type": "integer", "description": "超时秒数", "default": 30},
                    },
                    "required": ["code"],
                },
            },
        },
    },
    # --- 企业级功能 ---
    {
        "module": "auth",
        "skill_id": "access_control",
        "title": "权限管理",
        "description": "权限管理 RBAC 访问控制 角色权限 auth 用户权限 权限检查 访问控制",
        "schema": {
            "type": "function",
            "function": {
                "name": "access_control",
                "description": "RBAC权限管理：角色定义、权限分配和权限检查。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：list_roles/check_permission/assign", "default": "list_roles"},
                        "user_id": {"type": "string", "description": "用户ID", "default": ""},
                        "permission": {"type": "string", "description": "权限名", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "backup",
        "skill_id": "backup_restore",
        "title": "备份恢复",
        "description": "备份 数据备份 恢复 灾难恢复 backup 备份系统 数据恢复 增量备份",
        "schema": {
            "type": "function",
            "function": {
                "name": "backup_restore",
                "description": "备份恢复系统：全量/增量备份、备份加密、版本管理和灾难恢复。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：create/restore/list/delete", "default": "list"},
                        "backup_id": {"type": "string", "description": "备份ID（restore/delete时使用）", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "webhooks",
        "skill_id": "webhook_manage",
        "title": "Webhook管理",
        "description": "Webhook 事件订阅 消息推送 webhooks 钩子 事件通知 回调",
        "schema": {
            "type": "function",
            "function": {
                "name": "webhook_manage",
                "description": "Webhook系统管理：事件订阅、端点配置、消息签名验证。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：list/subscribe/unsubscribe/test", "default": "list"},
                        "event_type": {"type": "string", "description": "事件类型", "default": ""},
                        "endpoint_url": {"type": "string", "description": "回调URL", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "tasks",
        "skill_id": "task_queue",
        "title": "异步任务队列",
        "description": "异步任务 任务队列 后台任务 tasks 异步执行 任务调度 任务状态",
        "schema": {
            "type": "function",
            "function": {
                "name": "task_queue",
                "description": "异步任务队列：提交后台任务、查询任务状态和结果。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：submit/status/result/cancel/list", "default": "list"},
                        "task_id": {"type": "string", "description": "任务ID", "default": ""},
                        "task_type": {"type": "string", "description": "任务类型", "default": ""},
                        "params": {"type": "string", "description": "任务参数（JSON）", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "ws",
        "skill_id": "websocket_push",
        "title": "WebSocket推送",
        "description": "WebSocket 实时推送 消息推送 ws 实时通知 长连接",
        "schema": {
            "type": "function",
            "function": {
                "name": "websocket_push",
                "description": "WebSocket实时消息推送：向客户端推送实时通知和消息。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：broadcast/send/rooms/connections", "default": "connections"},
                        "room": {"type": "string", "description": "房间名", "default": ""},
                        "message": {"type": "string", "description": "消息内容", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "export",
        "skill_id": "data_export",
        "title": "数据导出",
        "description": "数据导出 导出会话 导出记忆 export 备份数据 导出数据",
        "schema": {
            "type": "function",
            "function": {
                "name": "data_export",
                "description": "数据导出：导出会话历史、长期记忆、配置和审计日志。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "导出类型：sessions/memory/config/audit", "default": "sessions"},
                        "format": {"type": "string", "description": "格式：json/markdown/zip", "default": "json"},
                    },
                },
            },
        },
    },
    {
        "module": "data_lineage",
        "skill_id": "data_lineage_track",
        "title": "数据血缘",
        "description": "数据血缘 数据流转 影响分析 数据追踪 data_lineage 数据治理 血缘关系",
        "schema": {
            "type": "function",
            "function": {
                "name": "data_lineage_track",
                "description": "数据血缘追踪：数据流转路径、字段级血缘、影响分析。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：trace/impact/visualize", "default": "trace"},
                        "node_id": {"type": "string", "description": "节点ID", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "security",
        "skill_id": "security_scan",
        "title": "安全扫描",
        "description": "安全扫描 漏洞扫描 安全审计 加密 security 输入验证 安全检查",
        "schema": {
            "type": "function",
            "function": {
                "name": "security_scan",
                "description": "安全扫描：漏洞检测、安全审计、输入验证和数据加密。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：scan/audit/encrypt/validate", "default": "scan"},
                        "target": {"type": "string", "description": "扫描目标", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "performance",
        "skill_id": "performance_opt",
        "title": "性能优化",
        "description": "性能优化 缓存 连接池 性能监控 performance 加速 优化性能",
        "schema": {
            "type": "function",
            "function": {
                "name": "performance_opt",
                "description": "性能优化：多级缓存、连接池管理和性能监控分析。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：cache_stats/pool_stats/profile/optimize", "default": "cache_stats"},
                    },
                },
            },
        },
    },
    # --- 前沿探索 ---
    {
        "module": "smart_contract",
        "skill_id": "contract_generate",
        "title": "智能合约",
        "description": "智能合约 Solidity 区块链 合约审计 smart_contract ERC20 ERC721 Web3 以太坊",
        "schema": {
            "type": "function",
            "function": {
                "name": "contract_generate",
                "description": "智能合约生成与审计：从自然语言生成Solidity合约，检测安全漏洞，Gas优化。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：generate/audit/optimize/test", "default": "generate"},
                        "description": {"type": "string", "description": "合约需求描述", "default": ""},
                        "code": {"type": "string", "description": "合约代码（audit/optimize时使用）", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "spatial_3d",
        "skill_id": "scene_3d",
        "title": "3D场景",
        "description": "3D场景 三维空间 场景管理 spatial_3d Three.js 3D可视化 虚拟空间",
        "schema": {
            "type": "function",
            "function": {
                "name": "scene_3d",
                "description": "3D空间交互：场景创建、物体管理、空间对话和AR/VR适配。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：create/add_object/export/describe", "default": "create"},
                        "scene_id": {"type": "string", "description": "场景ID", "default": ""},
                        "object_type": {"type": "string", "description": "物体类型", "default": "cube"},
                    },
                },
            },
        },
    },
    {
        "module": "kg_visualizer",
        "skill_id": "knowledge_graph",
        "title": "知识图谱",
        "description": "知识图谱 图谱可视化 实体关系 kg_visualizer 图谱查询 关系网络",
        "schema": {
            "type": "function",
            "function": {
                "name": "knowledge_graph",
                "description": "知识图谱可视化：实体关系管理、图谱查询和可视化导出。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：query/neighbors/visualize/export", "default": "query"},
                        "entity": {"type": "string", "description": "实体名称", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "analytics",
        "skill_id": "conversation_analytics",
        "title": "对话分析",
        "description": "对话分析 统计分析 用户画像 情感分析 analytics 数据洞察 趋势分析",
        "schema": {
            "type": "function",
            "function": {
                "name": "conversation_analytics",
                "description": "对话分析与洞察：消息统计、Token用量、用户画像、情感分析和话题聚类。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：stats/profile/sentiment/topics", "default": "stats"},
                    },
                },
            },
        },
    },
    {
        "module": "ux",
        "skill_id": "ux_settings",
        "title": "用户体验",
        "description": "用户体验 主题切换 快捷命令 ux 界面设置 快捷键 个性化",
        "schema": {
            "type": "function",
            "function": {
                "name": "ux_settings",
                "description": "用户体验设置：主题切换、快捷命令和个性化配置。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：theme/shortcuts/help", "default": "help"},
                        "value": {"type": "string", "description": "设置值", "default": ""},
                    },
                },
            },
        },
    },
    {
        "module": "plugin_sdk",
        "skill_id": "plugin_dev",
        "title": "插件开发",
        "description": "插件开发 SDK 脚手架 插件测试 plugin_sdk 开发插件 创建插件 插件模板",
        "schema": {
            "type": "function",
            "function": {
                "name": "plugin_dev",
                "description": "插件开发SDK：生成插件脚手架、测试插件、生成文档和打包发布。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "操作：scaffold/test/docs/list_templates", "default": "list_templates"},
                        "template": {"type": "string", "description": "模板类型：basic/skill/api_extension/gateway", "default": "basic"},
                        "name": {"type": "string", "description": "插件名称", "default": ""},
                    },
                },
            },
        },
    },
]


class ModuleAutoLoader:
    """模块自动加载器 — 为可选模块注册代理技能，实现按需自动加载。

    在 SkillManager 中为每个可选模块注册一个代理技能。
    当 LLM 调用代理技能时，自动加载对应模块并委托执行。
    """

    def __init__(
        self,
        registry: "ModuleRegistry",
        skills: "SkillManager",
    ) -> None:
        self._registry = registry
        self._skills = skills
        self._registered_skills: Dict[str, str] = {}  # skill_id -> module_name
        self._ctx: Optional["AgentContext"] = None

    def set_context(self, ctx: "AgentContext") -> None:
        """设置 AgentContext，供加载模块时使用。"""
        self._ctx = ctx

    def register_proxy_skills(self) -> int:
        """为所有可选模块注册代理技能到 SkillManager。

        Returns:
            注册的代理技能数量。
        """
        from skills import Skill

        count = 0
        for spec in MODULE_SKILLS:
            module_name = spec["module"]
            skill_id = spec["skill_id"]

            # 检查模块是否被禁用
            entry = self._registry.get_entry(module_name)
            if entry is None:
                continue

            # 跳过已注册的技能（避免重复）
            if self._skills.get(skill_id) is not None:
                continue

            # 创建代理技能
            proxy_skill = Skill(
                id=skill_id,
                title=spec["title"],
                description=spec["description"],
                schema=spec["schema"],
                handler=self._make_proxy_handler(module_name, skill_id),
            )
            self._skills.register(proxy_skill)
            self._registered_skills[skill_id] = module_name
            count += 1

        logger.info("registered %d proxy skills for lazy modules", count)
        return count

    def _make_proxy_handler(self, module_name: str, skill_id: str):
        """创建代理技能的 handler — 自动加载模块后委托执行。"""

        async def _proxy_handler(args: Dict[str, Any]) -> str:
            """代理技能 handler：自动加载模块并执行。"""
            # 1. 检查模块是否已加载
            if not self._registry.is_loaded(module_name):
                logger.info("auto-loading module '%s' for skill '%s'", module_name, skill_id)
                if self._ctx is None:
                    return f"[模块 {module_name} 无法加载：上下文未初始化]"

                # 按需加载模块
                plugin = await self._registry.get(module_name, self._ctx)
                if plugin is None:
                    entry = self._registry.get_entry(module_name)
                    if entry and entry.load_policy.value == "off":
                        return f"[模块 {module_name} 已禁用，请在配置中启用]"
                    return f"[模块 {module_name} 加载失败: {entry._error if entry else '未知错误'}]"

            # 2. 获取已加载的模块实例
            entry = self._registry.get_entry(module_name)
            if entry is None or entry._instance is None:
                return f"[模块 {module_name} 未加载]"

            plugin = entry._instance

            # 3. 委托给模块的实际功能
            return await self._delegate_to_module(plugin, module_name, skill_id, args)

        return _proxy_handler

    async def _delegate_to_module(
        self,
        plugin: Any,
        module_name: str,
        skill_id: str,
        args: Dict[str, Any],
    ) -> str:
        """将技能调用委托给已加载模块的实际功能。"""
        try:
            action = args.get("action", "")

            # 通用辅助：调用方法并处理 sync/async 返回
            async def _call(method, *a, **kw):
                result = method(*a, **kw)
                if asyncio.iscoroutine(result):
                    result = await result
                return result

            # 根据模块类型分派到对应方法
            if module_name == "multimodal":
                if skill_id == "image_generate":
                    result = await _call(plugin.generate_image,
                                         args.get("prompt", ""),
                                         size=args.get("size", "square"),
                                         model=args.get("model", "") or None)
                    return str(result) if result else "[图像生成完成]"
                elif skill_id == "image_describe":
                    result = await _call(plugin.describe_image,
                                         args.get("image_url", ""),
                                         question=args.get("question", ""))
                    return str(result) if result else "[图像分析完成]"
                elif skill_id == "text_to_speech":
                    result = await _call(plugin.text_to_speech,
                                         args.get("text", ""),
                                         voice=args.get("voice", "") or None)
                    return str(result) if result else "[语音生成完成]"

            elif module_name == "monitor":
                if action in ("metrics", ""):
                    metrics = await _call(plugin.collect_metrics)
                    return str(metrics)
                elif action == "health":
                    return str({"status": "healthy"})

            elif module_name == "marketplace":
                if action in ("list", ""):
                    return str(await _call(plugin.list_installed))
                elif action == "search":
                    return str(await _call(plugin.browse_registry, args.get("query", "")))
                elif action == "install":
                    return str(await _call(plugin.install, args.get("skill_id", "")))

            elif module_name == "alerting":
                if action in ("list_rules", ""):
                    return str(await _call(plugin.list_rules))
                elif action == "list_alerts":
                    return str(await _call(plugin.list_history))

            elif module_name == "rag":
                if skill_id == "knowledge_search":
                    # search 需要 kb_id，先获取可用的知识库列表
                    kbs = plugin.list_knowledge_bases()
                    if not kbs:
                        return "[暂无知识库，请先上传文档创建知识库]"
                    kb_id = kbs[0].id  # 使用第一个知识库
                    result = await _call(plugin.search, args.get("query", ""),
                                         kb_id, top_k=args.get("top_k", 5))
                    return str(result)

            elif module_name == "automl":
                return str({"status": "automl ready", "action": action})

            elif module_name == "active_learning":
                learner = getattr(plugin, "_learner", None) or plugin
                if action == "progress":
                    return str(await _call(getattr(learner, "get_progress", lambda: {})))
                elif action == "knowledge":
                    return str(await _call(getattr(learner, "get_knowledge", lambda: [])))
                elif action == "gaps":
                    return str(await _call(getattr(learner, "identify_gaps", lambda: [])))
                elif action == "plan":
                    return str(await _call(getattr(learner, "generate_plan", lambda: {})))

            elif module_name == "document_intelligence":
                text = args.get("text", "")
                if action == "form":
                    return str(await _call(plugin.recognize_form, text))
                elif action == "receipt":
                    return str(await _call(plugin.recognize_receipt, text))
                elif action == "contract":
                    return str(await _call(plugin.review_contract, text))
                elif action in ("vqa", ""):
                    # ask_document 签名: (question, document)
                    return str(await _call(plugin.ask_document, args.get("question", ""), text))
                elif action == "table":
                    return str(await _call(plugin.recognize_tables, text))
                elif action == "layout":
                    return str(await _call(plugin.analyze_layout, text))

            elif module_name == "multi_agent":
                return str({"status": "multi_agent ready", "task": args.get("task", "")})

            elif module_name == "recommender":
                user_id = args.get("user_id", "")
                method = args.get("method", "cf_user")
                top_k = args.get("top_k", 10)
                recs = await _call(plugin.recommend, user_id, method, top_k)
                if isinstance(recs, list):
                    return str([{"item": r.item_id, "score": round(r.score, 3), "reason": r.reason}
                                for r in recs])
                return str(recs)

            elif module_name == "prompt_studio":
                if action in ("list", ""):
                    return str(await _call(getattr(plugin, "list_templates", lambda **kw: [])))
                elif action == "create":
                    return str(await _call(getattr(plugin, "create_template", lambda **kw: {}),
                                           name=args.get("content", "")[:50] or "untitled",
                                           description=args.get("content", ""),
                                           system_prompt=args.get("content", "")))
                elif action == "test":
                    return str(await _call(getattr(plugin, "get_versions", lambda tid: []), args.get("template_id", "")))
                elif action == "optimize":
                    return str(await _call(getattr(plugin, "list_ab_tests", lambda: [])))

            elif module_name == "agent_templates":
                if action in ("list", ""):
                    return str(await _call(getattr(plugin, "list_templates", lambda **kw: [])))
                elif action == "search":
                    # list_templates 签名: (category=None, tag=None, search=None)
                    return str(await _call(plugin.list_templates, search=args.get("category", "")))
                elif action == "apply":
                    return str(await _call(getattr(plugin, "apply_template", lambda tid: {}), args.get("template_id", "")))

            elif module_name == "code_interpreter":
                code = args.get("code", "")
                # CodeInterpreterPlugin 没有 execute 方法，使用 execute_and_format(code, session_id=None)
                result = await _call(plugin.execute_and_format, code)
                return str(result)

            elif module_name == "backup":
                if action in ("list", ""):
                    return str(await _call(getattr(plugin, "list_backups", lambda: [])))
                elif action == "create":
                    return str(await _call(getattr(plugin, "create_backup", lambda: "ok")))

            elif module_name == "export":
                return str({"status": "export ready", "action": action})

            elif module_name == "data_lineage":
                return str({"status": "data_lineage ready", "action": action})

            elif module_name == "security":
                return str({"status": "security ready", "action": action})

            elif module_name == "performance":
                return str({"status": "performance ready", "action": action})

            elif module_name == "smart_contract":
                if action == "generate":
                    return str(await _call(getattr(plugin, "generate_contract", lambda d: ""), args.get("description", "")))
                elif action == "audit":
                    return str(await _call(getattr(plugin, "audit_contract", lambda c: ""), args.get("code", "")))

            elif module_name == "spatial_3d":
                return str({"status": "spatial_3d ready", "action": action})

            elif module_name == "kg_visualizer":
                return str({"status": "kg_visualizer ready", "action": action})

            elif module_name == "analytics":
                if action in ("stats", ""):
                    return str({"status": "analytics ready", "message": "使用 analyze_conversation 分析对话"})
                elif action == "sentiment":
                    return str(await _call(plugin.analyze_sentiment, args.get("text", "")))

            elif module_name == "plugin_sdk":
                if action in ("list_templates", ""):
                    return str(await _call(plugin.list_templates))
                elif action == "scaffold":
                    scaffolder = getattr(plugin, "_scaffolder", None)
                    if scaffolder:
                        return str(await _call(scaffolder.create, args.get("template", "basic"), args.get("name", "my_plugin")))
                    return "[脚手架未初始化]"

            # 通用回退：返回模块状态
            return f"[模块 {module_name} 已加载，技能 {skill_id} 调用完成。action={action}]"

        except Exception as exc:
            logger.error("proxy skill '%s' delegation failed: %s", skill_id, exc, exc_info=True)
            return f"[模块 {module_name} 执行出错: {exc}]"

    def get_registered_skills(self) -> Dict[str, str]:
        """获取已注册的代理技能映射。"""
        return dict(self._registered_skills)

    def get_skill_count(self) -> int:
        """获取已注册的代理技能数量。"""
        return len(self._registered_skills)
