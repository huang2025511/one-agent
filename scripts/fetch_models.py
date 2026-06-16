#!/usr/bin/env python3
"""Fetch available models from LLM providers in real-time."""

import argparse
import json
import sys
import urllib.request
import urllib.error
from typing import List, Optional


def fetch_openai_models(api_key: str, base_url: str = None) -> List[str]:
    """Fetch available models from OpenAI-compatible API."""
    url = base_url or "https://api.openai.com/v1/models"
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            # Filter and sort chat models
            chat_models = sorted([
                m for m in models
                if any(x in m.lower() for x in ["gpt-4", "gpt-3.5", "gpt-35", "claude", "gemini", "llama", "mistral", "qwen", "deepseek"])
            ], key=lambda x: (
                0 if "gpt-4o" in x.lower() else
                1 if "gpt-4" in x.lower() else
                2 if "claude" in x.lower() else
                3 if "gemini" in x.lower() else
                4
            ))
            return chat_models if chat_models else sorted(models[:10])
    except Exception as e:
        return [f"Error: {e}"]


def fetch_deepseek_models(api_key: str) -> List[str]:
    """Fetch available models from DeepSeek."""
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            return sorted(models)
    except Exception as e:
        return [f"Error: {e}"]


def fetch_sensenova_models(api_key: str) -> List[str]:
    """Fetch available models from SenseNova (商汤科技)."""
    try:
        req = urllib.request.Request(
            "https://token.sensenova.cn/v1/models",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            return sorted(models)
    except Exception as e:
        return [f"Error: {e}"]


def fetch_dashscope_models(api_key: str) -> List[str]:
    """Fetch available models from DashScope (阿里云)."""
    try:
        req = urllib.request.Request(
            "https://dashscope.aliyuncs.com/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            return sorted(models)
    except Exception as e:
        return [f"Error: {e}"]


def fetch_anthropic_models(api_key: str) -> List[str]:
    """Fetch available models from Anthropic."""
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["id"] for m in data.get("data", [])]
            return sorted(models)
    except Exception as e:
        # Fallback to known models
        return ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"]


def fetch_gemini_models(api_key: str) -> List[str]:
    """Fetch available models from Google Gemini."""
    try:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["name"].split("/")[-1] for m in data.get("models", [])]
            return sorted(models)
    except Exception as e:
        return [f"Error: {e}"]


def fetch_ollama_models(base_url: str = "http://localhost:11434") -> List[str]:
    """Fetch available models from Ollama."""
    try:
        req = urllib.request.Request(f"{base_url}/api/tags")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            return sorted(models)
    except Exception as e:
        return [f"Error: {e}"]


def fetch_custom_models(api_key: str, base_url: str) -> List[str]:
    """Fetch models from custom OpenAI-compatible API."""
    return fetch_openai_models(api_key, base_url)


# Provider configurations
PROVIDERS = {
    "openai": {
        "name": "OpenAI",
        "fetch": fetch_openai_models,
        "default_models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "needs_key": True,
        "needs_url": False,
    },
    "deepseek": {
        "name": "DeepSeek (深度求索)",
        "fetch": fetch_deepseek_models,
        "default_models": ["deepseek-chat", "deepseek-reasoner", "deepseek-coder"],
        "needs_key": True,
        "needs_url": False,
    },
    "sensenova": {
        "name": "SenseNova (商汤)",
        "fetch": fetch_sensenova_models,
        "default_models": ["deepseek-v4-flash", "sensenova-default", "deepseek-v4"],
        "needs_key": True,
        "needs_url": False,
    },
    "dashscope": {
        "name": "DashScope (阿里云)",
        "fetch": fetch_dashscope_models,
        "default_models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"],
        "needs_key": True,
        "needs_url": False,
    },
    "anthropic": {
        "name": "Anthropic (Claude)",
        "fetch": fetch_anthropic_models,
        "default_models": ["claude-opus-4-20250514", "claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"],
        "needs_key": True,
        "needs_url": False,
    },
    "gemini": {
        "name": "Google Gemini",
        "fetch": fetch_gemini_models,
        "default_models": ["gemini-1.5-pro", "gemini-1.5-flash", "gemini-pro"],
        "needs_key": True,
        "needs_url": False,
    },
    "ollama": {
        "name": "Ollama (本地模型)",
        "fetch": fetch_ollama_models,
        "default_models": ["llama3", "llama3.1", "qwen2.5:7b", "codellama", "mistral"],
        "needs_key": False,
        "needs_url": True,
        "default_url": "http://localhost:11434",
    },
    "groq": {
        "name": "Groq",
        "fetch": lambda key: fetch_openai_models(key, "https://api.groq.com/openai/v1/models"),
        "default_models": ["llama-3.1-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        "needs_key": True,
        "needs_url": False,
    },
    "together": {
        "name": "Together AI",
        "fetch": lambda key: fetch_openai_models(key, "https://api.together.xyz/v1/models"),
        "default_models": ["meta-llama/Llama-3-70b-chat", "mistralai/Mixtral-8x7B-Instruct-v0.1"],
        "needs_key": True,
        "needs_url": False,
    },
    "fireworks": {
        "name": "Fireworks AI",
        "fetch": lambda key: fetch_openai_models(key, "https://api.fireworks.ai/inference/v1/models"),
        "default_models": ["accounts/fireworks/models/llama-v3-70b-instruct"],
        "needs_key": True,
        "needs_url": False,
    },
    "cohere": {
        "name": "Cohere",
        "fetch": lambda key: fetch_openai_models(key, "https://api.cohere.ai/v1/models"),
        "default_models": ["command-r-plus", "command-r", "command"],
        "needs_key": True,
        "needs_url": False,
    },
    "mistral": {
        "name": "Mistral AI",
        "fetch": lambda key: fetch_openai_models(key, "https://api.mistral.ai/v1/models"),
        "default_models": ["mistral-large-latest", "mistral-small-latest", "open-mixtral-8x7b"],
        "needs_key": True,
        "needs_url": False,
    },
}


def list_providers():
    """List all available providers."""
    print("可用服务商:")
    for i, (key, cfg) in enumerate(PROVIDERS.items(), 1):
        print(f"  {i}) {cfg['name']} ({key})")
    print(f"  {len(PROVIDERS) + 1}) 自定义 OpenAI 兼容 API")
    print(f"  {len(PROVIDERS) + 2}) 自定义 API 地址")


def fetch_models(provider: str, api_key: str = None, base_url: str = None) -> tuple[List[str], str]:
    """Fetch models based on provider.
    
    Returns:
        (models_list, provider_name)
    """
    # Custom provider
    if provider == "custom-openai":
        if not base_url:
            return [], "Custom OpenAI"
        return fetch_custom_models(api_key or "dummy", base_url), f"custom-{base_url}"
    
    if provider == "custom-url":
        return [], "Custom"
    
    # Known provider
    cfg = PROVIDERS.get(provider.lower())
    if cfg:
        if cfg.get("needs_key", True) and not api_key:
            return cfg["default_models"], cfg["name"]
        
        # Use default URL for Ollama if not specified
        if provider == "ollama" and not base_url:
            base_url = cfg.get("default_url", "http://localhost:11434")
        
        try:
            if provider == "ollama":
                models = cfg["fetch"](base_url)
            elif provider == "gemini":
                models = cfg["fetch"](api_key)
            else:
                models = cfg["fetch"](api_key)
            
            if models and not models[0].startswith("Error"):
                return models, cfg["name"]
        except Exception:
            pass
        
        return cfg["default_models"], cfg["name"]
    
    return [], provider


def main():
    parser = argparse.ArgumentParser(description="获取服务商可用模型列表")
    parser.add_argument("--provider", required=True, help="服务商名称")
    parser.add_argument("--key", help="API Key")
    parser.add_argument("--url", help="API Base URL (用于自定义服务商)")
    parser.add_argument("--list", action="store_true", help="列出所有服务商")
    args = parser.parse_args()
    
    if args.list:
        list_providers()
        return
    
    models, name = fetch_models(args.provider, args.key, args.url)
    
    if models:
        print("\n".join(models))
    else:
        print("无法获取模型列表", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
