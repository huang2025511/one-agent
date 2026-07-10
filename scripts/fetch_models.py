#!/usr/bin/env python3
"""Fetch available models from LLM providers in real-time."""

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

# Custom providers storage
CUSTOM_PROVIDERS_FILE = Path.home() / ".one-agent" / "custom_providers.json"


def load_custom_providers() -> Dict[str, dict]:
    """Load custom providers from file."""
    if CUSTOM_PROVIDERS_FILE.exists():
        try:
            with open(CUSTOM_PROVIDERS_FILE) as f:
                return json.load(f)
        except Exception as exc:
            logger.debug("ignored non-critical error: %s", exc)
    return {}


def save_custom_provider(name: str, base_url: str, api_key: str = None) -> None:
    """Save a custom provider to file."""
    CUSTOM_PROVIDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    providers = load_custom_providers()

    # Generate a unique key
    key = name.lower().replace(" ", "-").replace("/", "-")
    providers[key] = {
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
    }

    with open(CUSTOM_PROVIDERS_FILE, "w") as f:
        json.dump(providers, f, indent=2)


def _fetch_models_generic(
    url: str,
    api_key: str = None,
    headers: Dict[str, str] = None,
    model_key: str = "data",
    id_field: str = "id",
    name_prefix: str = "",
) -> List[str]:
    """Generic model fetcher for OpenAI-compatible /v1/models endpoints.

    Args:
        url: API endpoint URL.
        api_key: API key for Authorization header (None = no auth).
        headers: Extra headers (merged with Authorization if api_key set).
        model_key: JSON key containing the model list ("data" or "models").
        id_field: Field to extract from each model entry ("id" or "name").
        name_prefix: Strip this prefix from the extracted name (e.g. "models/" for Gemini).

    Returns:
        Sorted list of model names, or ["Error: ..."] on failure.
    """
    hdrs = dict(headers or {})
    if api_key:
        hdrs.setdefault("Authorization", f"Bearer {api_key}")
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = []
            for m in data.get(model_key, []):
                name = m[id_field]
                if name_prefix and name.startswith(name_prefix):
                    name = name[len(name_prefix):]
                models.append(name)
            return sorted(models)
    except Exception as e:
        return [f"Error: {e}"]


def fetch_openai_models(api_key: str = None, base_url: str = None) -> List[str]:
    """Fetch available models from OpenAI-compatible API."""
    url = base_url or "https://api.openai.com/v1/models"
    models = _fetch_models_generic(url, api_key=api_key)
    if models and not models[0].startswith("Error:"):
        chat_models = sorted([
            m for m in models
            if any(x in m.lower() for x in ["gpt-", "claude", "gemini", "llama", "mistral", "qwen", "deepseek", "command", "mixtral"])
        ], key=lambda x: (
            0 if "gpt-4o" in x.lower() else
            1 if "gpt-4" in x.lower() else
            2 if "claude" in x.lower() else
            3 if "gemini" in x.lower() else
            4
        ))
        return chat_models if chat_models else sorted(models[:10])
    return models


def fetch_deepseek_models(api_key: str) -> List[str]:
    """Fetch available models from DeepSeek."""
    return _fetch_models_generic("https://api.deepseek.com/v1/models", api_key=api_key)


def fetch_sensenova_models(api_key: str) -> List[str]:
    """Fetch available models from SenseNova (商汤科技)."""
    return _fetch_models_generic("https://token.sensenova.cn/v1/models", api_key=api_key)


def fetch_dashscope_models(api_key: str) -> List[str]:
    """Fetch available models from DashScope (阿里云通义千问)."""
    return _fetch_models_generic(
        "https://dashscope.aliyuncs.com/api/v1/models",
        api_key=api_key,
        headers={"Accept": "application/json"},
    )


def fetch_anthropic_models(api_key: str) -> List[str]:
    """Fetch available models from Anthropic (Claude)."""
    return _fetch_models_generic(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api_key},
    )


def fetch_gemini_models(api_key: str) -> List[str]:
    """Fetch available models from Google Gemini."""
    return _fetch_models_generic(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
        model_key="models",
        id_field="name",
        name_prefix="models/",
    )


def fetch_ollama_models(base_url: str = "http://localhost:11434") -> List[str]:
    """Fetch available models from Ollama (本地模型)."""
    return _fetch_models_generic(
        f"{base_url}/api/tags",
        model_key="models",
        id_field="name",
    )


# Provider configurations - 完整的预设服务商列表
PROVIDERS: Dict[str, dict] = {
    # ===== 国际主流服务商 =====
    "openai": {
        "name": "OpenAI",
        "desc": "GPT 系列模型",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.openai.com/v1/models"),
        "default_models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "needs_key": True,
        "needs_url": False,
    },
    "anthropic": {
        "name": "Anthropic",
        "desc": "Claude 系列模型",
        "fetch": lambda key, url: fetch_anthropic_models(key),
        "default_models": ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"],
        "needs_key": True,
        "needs_url": False,
    },
    "google": {
        "name": "Google Gemini",
        "desc": "Gemini 系列模型",
        "fetch": lambda key, url: fetch_gemini_models(key),
        "default_models": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash-exp"],
        "needs_key": True,
        "needs_url": False,
    },
    "mistral": {
        "name": "Mistral AI",
        "desc": "Mistral / Mixtral 模型",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.mistral.ai/v1/models"),
        "default_models": ["mistral-large-latest", "mistral-small-latest", "open-mixtral-8x7b"],
        "needs_key": True,
        "needs_url": False,
    },
    "cohere": {
        "name": "Cohere",
        "desc": "Command R 系列",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.cohere.ai/v1/models"),
        "default_models": ["command-r-plus", "command-r", "command"],
        "needs_key": True,
        "needs_url": False,
    },
    "meta": {
        "name": "Meta AI",
        "desc": "Llama 系列开源模型",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["llama-3.1-70b", "llama-3.1-8b", "llama-3-70b"],
        "needs_key": True,
        "needs_url": False,
    },

    # ===== 国内服务商 =====
    "deepseek": {
        "name": "DeepSeek",
        "desc": "深度求索大模型",
        "fetch": lambda key, url: fetch_deepseek_models(key),
        "default_models": ["deepseek-chat", "deepseek-reasoner", "deepseek-coder"],
        "needs_key": True,
        "needs_url": False,
    },
    "sensenova": {
        "name": "SenseNova",
        "desc": "商汤科技大模型",
        "fetch": lambda key, url: fetch_sensenova_models(key),
        "default_models": ["deepseek-v4-flash", "sensenova-default", "deepseek-v4"],
        "needs_key": True,
        "needs_url": False,
    },
    "dashscope": {
        "name": "DashScope",
        "desc": "阿里云通义千问",
        "fetch": lambda key, url: fetch_dashscope_models(key),
        "default_models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long", "qwen2.5-72b"],
        "needs_key": True,
        "needs_url": False,
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "desc": "硅基流动 (SiliconCloud)",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.siliconflow.cn/v1/models"),
        "default_models": ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V2.5"],
        "needs_key": True,
        "needs_url": False,
    },
    "zhipuai": {
        "name": "ZhipuAI",
        "desc": "智谱AI (GLM)",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://open.bigmodel.cn/api/paas/v4/models"),
        "default_models": ["glm-4", "glm-4-flash", "glm-4-plus", "glm-3-turbo"],
        "needs_key": True,
        "needs_url": False,
    },
    "baichuan": {
        "name": "Baichuan",
        "desc": "百川大模型",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.baichuan-ai.com/v1/models"),
        "default_models": ["Baichuan4", "Baichuan3-Turbo", "Baichuan3-Turbo-128k"],
        "needs_key": True,
        "needs_url": False,
    },
    "minimax": {
        "name": "MiniMax",
        "desc": "稀宇科技 (海螺AI)",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.minimax.chat/v1/models"),
        "default_models": ["abab6.5s-chat", "abab5.5s-chat"],
        "needs_key": True,
        "needs_url": False,
    },
    "stepfun": {
        "name": "StepFun",
        "desc": "阶跃星辰 (Step)",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.stepfun.com/v1/models"),
        "default_models": ["step-1v-8k", "step-1v-32k", "step-1o-mini"],
        "needs_key": True,
        "needs_url": False,
    },
    "moonshot": {
        "name": "Moonshot",
        "desc": "月之暗面 (Kimi)",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.moonshot.cn/v1/models"),
        "default_models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "needs_key": True,
        "needs_url": False,
    },
    "spark": {
        "name": "iFlytek Spark",
        "desc": "讯飞星火认知大模型",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["Spark4.0 Ultra", "Spark3.5 Max", "Spark3.5 Pro"],
        "needs_key": True,
        "needs_url": False,
    },
    "wenxin": {
        "name": "Baidu Wenxin",
        "desc": "百度文心一言 (ERNIE)",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["ernie-4.0-8k-latest", "ernie-3.5-8k-latest", "ernie-bot"],
        "needs_key": True,
        "needs_url": False,
    },
    "hunyuan": {
        "name": "Tencent Hunyuan",
        "desc": "腾讯混元大模型",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["hunyuan", "hunyuan-pro"],
        "needs_key": True,
        "needs_url": False,
    },

    # ===== 高性价比 / 开源 =====
    "groq": {
        "name": "Groq",
        "desc": "Llama / Mixtral 高速推理",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.groq.com/openai/v1/models"),
        "default_models": ["llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        "needs_key": True,
        "needs_url": False,
    },
    "together": {
        "name": "Together AI",
        "desc": "Llama / 开源模型平台",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.together.xyz/v1/models"),
        "default_models": ["meta-llama/Llama-3.1-70B-Instruct", "mistralai/Mixtral-8x7B-Instruct-v0.1"],
        "needs_key": True,
        "needs_url": False,
    },
    "fireworks": {
        "name": "Fireworks AI",
        "desc": "Llama V3 / 高性能推理",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.fireworks.ai/inference/v1/models"),
        "default_models": ["accounts/fireworks/models/llama-v3-70b-instruct", "accounts/fireworks/models/llama-v3-8b-instruct"],
        "needs_key": True,
        "needs_url": False,
    },
    "replicate": {
        "name": "Replicate",
        "desc": "开源模型云部署",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["meta/meta-llama-3-70b-instruct", "mistralai/mistral-7b-instruct-v0.3"],
        "needs_key": True,
        "needs_url": False,
    },
    "anyscale": {
        "name": "Anyscale",
        "desc": "Llama / 开源模型托管",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.endpoints.anyscale.com/v1/models"),
        "default_models": ["meta-llama/Llama-3-70b-chat", "mistralai/Mistral-7B-Instruct-v0.1"],
        "needs_key": True,
        "needs_url": False,
    },
    "perplexity": {
        "name": "Perplexity",
        "desc": "在线搜索增强模型",
        "fetch": lambda key, url: fetch_openai_models(key, url or "https://api.perplexity.ai/models"),
        "default_models": ["sonar", "sonar-pro", "sonar-reasoning"],
        "needs_key": True,
        "needs_url": False,
    },
    "ollama": {
        "name": "Ollama",
        "desc": "本地模型运行",
        "fetch": lambda key, url: fetch_ollama_models(url or "http://localhost:11434"),
        "default_models": ["llama3", "llama3.1", "qwen2.5:7b", "codellama", "mistral", "deepseek-r1:7b"],
        "needs_key": False,
        "needs_url": True,
        "default_url": "http://localhost:11434",
    },

    # ===== 企业级 =====
    "azure": {
        "name": "Azure OpenAI",
        "desc": "微软 Azure OpenAI",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-35-turbo"],
        "needs_key": True,
        "needs_url": True,
    },
    "aws-bedrock": {
        "name": "AWS Bedrock",
        "desc": "亚马逊云 Bedrock",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["anthropic.claude-3-5-sonnet-20241022-v1:0", "meta.llama3-1-70b-instruct-v1:0"],
        "needs_key": True,
        "needs_url": True,
    },
    "vertex-ai": {
        "name": "Google Vertex AI",
        "desc": "谷歌云 Vertex AI",
        "fetch": lambda key, url: fetch_openai_models(key, url),
        "default_models": ["gemini-1.5-pro", "gemini-1.5-flash", "claude-3-5-sonnet@20241022"],
        "needs_key": True,
        "needs_url": True,
    },

    # ===== 自定义（运行时添加）=====
    "custom-openai": {
        "name": "自定义 OpenAI 兼容",
        "desc": "添加自定义 API",
        "fetch": lambda key, url: fetch_openai_models(key, url) if url else ["需要输入 URL"],
        "default_models": [],
        "needs_key": False,
        "needs_url": True,
    },
}


def list_providers(include_custom: bool = True):
    """List all available providers."""
    print("可用服务商列表:")
    print()

    # International
    print("【国际主流】")
    for key in ["openai", "anthropic", "google", "mistral", "cohere", "meta"]:
        if key in PROVIDERS:
            cfg = PROVIDERS[key]
            print(f"  {cfg['name']} - {cfg['desc']}")

    # China
    print()
    print("【国内服务商】")
    for key in ["deepseek", "sensenova", "dashscope", "siliconflow", "zhipuai", "baichuan", "minimax", "stepfun", "moonshot", "spark", "wenxin", "hunyuan"]:
        if key in PROVIDERS:
            cfg = PROVIDERS[key]
            print(f"  {cfg['name']} - {cfg['desc']}")

    # Open source / Budget
    print()
    print("【高性价比 / 开源】")
    for key in ["groq", "together", "fireworks", "replicate", "anyscale", "perplexity", "ollama"]:
        if key in PROVIDERS:
            cfg = PROVIDERS[key]
            print(f"  {cfg['name']} - {cfg['desc']}")

    # Enterprise
    print()
    print("【企业级】")
    for key in ["azure", "aws-bedrock", "vertex-ai"]:
        if key in PROVIDERS:
            cfg = PROVIDERS[key]
            print(f"  {cfg['name']} - {cfg['desc']}")

    # Custom
    if include_custom:
        custom = load_custom_providers()
        if custom:
            print()
            print("【已保存的自定义服务商】")
            for _key, cfg in custom.items():
                print(f"  {cfg['name']} - {cfg['base_url']}")


def fetch_models(provider: str, api_key: str = None, base_url: str = None) -> tuple[List[str], str]:
    """Fetch models based on provider.

    Returns:
        (models_list, provider_name)
    """
    # Check custom providers first
    custom = load_custom_providers()
    if provider in custom:
        cfg = custom[provider]
        models = fetch_openai_models(cfg.get("api_key"), cfg["base_url"])
        return models, cfg["name"]

    # Known provider
    cfg = PROVIDERS.get(provider.lower())
    if not cfg:
        return [], provider

    try:
        models = cfg["fetch"](api_key, base_url)
        if models and not models[0].startswith("Error"):
            return models, cfg["name"]
    except Exception as exc:
        logger.debug("ignored non-critical error: %s", exc)

    # Fallback to default models
    return cfg.get("default_models", []), cfg["name"]


def add_custom_provider(name: str, base_url: str, api_key: str = None) -> str:
    """Add a custom provider and return its key."""
    save_custom_provider(name, base_url, api_key)
    # Return the generated key
    return name.lower().replace(" ", "-").replace("/", "-")


def main():
    parser = argparse.ArgumentParser(description="获取服务商可用模型列表")
    parser.add_argument("--provider", help="服务商名称")
    parser.add_argument("--key", help="API Key")
    parser.add_argument("--url", help="API Base URL")
    parser.add_argument("--list", action="store_true", help="列出所有服务商")
    parser.add_argument("--add", metavar="NAME", help="添加自定义服务商")
    args = parser.parse_args()

    if args.list:
        list_providers()
        return

    if args.add:
        if not args.url:
            print("错误: --add 需要 --url 参数", file=sys.stderr)
            sys.exit(1)
        add_custom_provider(args.add, args.url, args.key)
        print(f"已添加自定义服务商: {args.add}")
        print("下次选择时输入编号 0 使用此服务商")
        return

    if not args.provider:
        print("错误: 需要 --provider 参数，或使用 --list 查看所有服务商", file=sys.stderr)
        sys.exit(1)

    models, name = fetch_models(args.provider, args.key, args.url)

    if models:
        print("\n".join(models))
    else:
        print("无法获取模型列表", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
