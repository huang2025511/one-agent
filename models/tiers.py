"""Model tier and cost constant definitions.

This module exists to avoid circular imports between models/__init__.py,
models/recommend.py, models/cost_tracker.py, and router/__init__.py.
"""

from typing import Dict, List

__all__ = ["MODEL_TIERS", "MODEL_COST"]

MODEL_TIERS: Dict[str, List[str]] = {
    "trivial": [
        "openrouter/meta-llama/llama-3-8b-instruct",
        "anthropic/claude-haiku-latest",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-7b-instruct",
        # 商汤实际可用模型（公测免费）：sensenova-6.7-flash-lite（对话+多模态）。
        # 修复：之前写的是 sensenova/tiny（虚构模型，调用返回 404，每次请求都
        # 浪费一次往返再 fallback）。改为真实模型名后不再 404。
        "sensenova/sensenova-6.7-flash-lite",
    ],
    "simple": [
        "anthropic/claude-3.5-haiku-20241022",
        "openai/gpt-4o-mini",
        "google/gemini-2.0-flash",
        "sensenova/sensenova-6.7-flash-lite",
    ],
    "complex": [
        "anthropic/claude-3.5-sonnet-20241022",
        "openai/gpt-4o",
        "google/gemini-2.5-pro-exp-03-25",
        # deepseek-v4-flash 是商汤托管的 DeepSeek V4 推理模型（商汤免费公测）。
        # 256K context + reasoning_effort 控制，适合复杂任务。
        "sensenova/deepseek-v4-flash",
    ],
    "expert": [
        "anthropic/claude-4.5-sonnet-20250514",
        "openai/o3",
        "google/gemini-2.5-pro-preview-05-15",
        # deepseek-v4-flash 已在 complex tier，不重复放入 expert。
        # 如果用户只配了 SenseNova key，expert 任务会 fallback 到 complex tier
        # 的 deepseek-v4-flash，这是当前可用的最强 SenseNova 模型。
        # 当用户配置了 anthropic/openai/google 的 key 时，expert 任务会
        # 优先使用 claude-4.5-sonnet / o3 / gemini-2.5-pro 等顶级模型。
        "kimi/kimi-k2-0711-preview",
    ],
}

# Rough per-token cost (USD per 1K tokens) for statistics
MODEL_COST: Dict[str, float] = {
    # Anthropic
    "anthropic/claude-3.5-sonnet-20241022": 0.003,
    "anthropic/claude-3.5-haiku-20241022":  0.0008,
    "anthropic/claude-haiku-latest":         0.0008,
    "anthropic/claude-4.5-sonnet-20250514":  0.003,
    # OpenAI
    "openai/gpt-4o":                0.005,
    "openai/gpt-4o-mini":           0.00015,
    "openai/gpt-4-turbo":           0.01,
    "openai/o3":                    0.015,
    "openai/o1":                    0.015,
    # Google
    "google/gemini-2.5-pro-exp-03-25":  0.00125,
    "google/gemini-2.0-flash":          0.0001,
    "google/gemini-2.5-pro-preview-05-15": 0.00125,
    # DeepSeek
    "deepseek/deepseek-chat":  0.00014,
    "deepseek/deepseek-reasoner": 0.00055,
    # Qwen / DashScope (Tongyi)
    "qwen/qwen-max":                  0.002,
    "qwen/qwen-plus":                 0.0008,
    "qwen/qwen-2.5-72b-instruct":     0.0004,
    "qwen/qwen-2.5-7b-instruct":      0.0001,
    # SenseNova (商汤)
    "sensenova/deepseek-v4-flash":    0.0001,
    "sensenova/sensenova-6.7-flash-lite": 0.0001,
    # Zhipu GLM (智谱)
    "glm/glm-4":                      0.001,
    "glm/glm-4-plus":                 0.001,
    # Moonshot / Kimi
    "kimi/kimi-k2-0711-preview":      0.0006,
    "kimi/moonshot-v1-128k":          0.001,
    # Yi (零一万物)
    "yi/yi-large":                    0.0008,
    # OpenRouter passthrough
    "openrouter/meta-llama/llama-3-8b-instruct": 0.0002,
}
