#!/usr/bin/env python3
"""Fetch available models from LLM providers in real-time."""

import argparse
import json
import sys
import urllib.request
import urllib.error
from typing import List, Optional


def fetch_openai_models(api_key: str) -> List[str]:
    """Fetch available models from OpenAI."""
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            # Filter only chat models
            models = [
                m["id"] for m in data.get("data", [])
                if any(x in m["id"] for x in ["gpt-4", "gpt-3.5", "gpt-35"])
            ]
            return sorted(models, key=lambda x: (
                0 if "gpt-4o" in x else
                1 if "gpt-4" in x else
                2
            ))
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
        # Fallback to known models
        return ["deepseek-v4-flash", "sensenova-default", "deepseek-v4"]


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


def fetch_models(provider: str, api_key: str) -> List[str]:
    """Fetch models based on provider."""
    fetchers = {
        "openai": fetch_openai_models,
        "deepseek": fetch_deepseek_models,
        "sensenova": fetch_sensenova_models,
        "dashscope": fetch_dashscope_models,
    }
    
    fetcher = fetchers.get(provider.lower())
    if fetcher:
        return fetcher(api_key)
    return []


def main():
    parser = argparse.ArgumentParser(description="获取服务商可用模型列表")
    parser.add_argument("--provider", required=True, help="服务商名称: openai, deepseek, sensenova, dashscope")
    parser.add_argument("--key", required=True, help="API Key")
    args = parser.parse_args()
    
    models = fetch_models(args.provider, args.key)
    
    if models:
        print("\n".join(models))
    else:
        print("无法获取模型列表", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
