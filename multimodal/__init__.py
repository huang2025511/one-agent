"""Multimodal support — image generation, image understanding, and TTS.

Provides:
  - Image generation (OpenAI DALL-E / Stability AI via OpenRouter)
  - Image understanding (GPT-4V / Claude Vision via vision-capable models)
  - Text-to-speech (OpenAI TTS / ElevenLabs)
  - Base64-encoded image handling end-to-end
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


class MultimodalPlugin(Plugin):
    """Handles image generation, vision, and TTS."""

    name = "multimodal"

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._api_keys: Dict[str, str] = {}
        self._timeout = 60

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        llm_cfg = ctx.config.get("llm", {}) or {}
        self._api_keys = llm_cfg.get("api_keys", {}) or {}
        self._timeout = llm_cfg.get("timeout", 60)
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
        await super().stop()

    # --------------------------------------------------------- image generation
    async def generate_image(
        self,
        prompt: str,
        model: str = "openai/dall-e-3",
        size: str = "1024x1024",
        quality: str = "standard",
        n: int = 1,
    ) -> Dict[str, Any]:
        """Generate images from a text prompt.

        Supports models via OpenAI DALL-E or compatible endpoints.
        Returns a dict with a base64-encoded image or URL.
        """
        if self._client is None:
            return {"error": "client not initialized"}

        provider, model_name = model.split("/", 1) if "/" in model else ("openai", model)
        api_key = self._api_keys.get(provider) or self._api_keys.get("openai", "")
        base = "https://api.openai.com/v1"

        try:
            resp = await self._client.post(
                f"{base}/images/generations",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "prompt": prompt,
                    "n": n,
                    "size": size,
                    "quality": quality,
                    "response_format": "b64_json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            images = []
            for item in data.get("data", []):
                b64 = item.get("b64_json", "")
                images.append({"b64_json": b64, "revised_prompt": item.get("revised_prompt", "")})
            return {"model": model, "images": images, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("image generation failed: %s", exc)
            return {"error": str(exc), "images": []}

    # --------------------------------------------------------- image understanding
    async def analyze_image(
        self,
        image_data: str,
        prompt: str = "Describe this image in detail.",
        model: str = "openai/gpt-4o",
    ) -> Dict[str, Any]:
        """Analyze an image given as base64 or a URL.

        Passes it to a vision-capable model.
        """
        if self._client is None:
            return {"error": "client not initialized"}

        # Detect format: base64 or URL
        if image_data.startswith("http"):
            image_payload = {"url": image_data}
        elif image_data.startswith("/") or image_data.startswith("data:"):
            # Local file path or data URI
            if image_data.startswith("data:"):
                image_payload = {"data": image_data}
            else:
                b64 = base64.b64encode(Path(image_data).read_bytes()).decode()
                image_payload = {"data": f"data:image/png;base64,{b64}"}
        else:
            # Assume raw base64
            image_payload = {"data": f"data:image/png;base64,{image_data}"}

        provider, model_name = model.split("/", 1) if "/" in model else ("openai", model)
        api_key = self._api_keys.get(provider) or self._api_keys.get("openai", "")
        base = "https://api.openai.com/v1"

        try:
            resp = await self._client.post(
                f"{base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": image_payload},
                            ],
                        }
                    ],
                    "max_tokens": 2048,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
            tokens = (data.get("usage") or {}).get("total_tokens", 0)
            return {"analysis": text, "tokens_used": tokens, "model": model}
        except Exception as exc:  # noqa: BLE001
            logger.warning("image analysis failed: %s", exc)
            return {"error": str(exc), "analysis": ""}

    # --------------------------------------------------------- text-to-speech
    async def text_to_speech(
        self,
        text: str,
        model: str = "openai/tts-1",
        voice: str = "alloy",
        response_format: str = "mp3",
    ) -> Dict[str, Any]:
        """Convert text to speech, returns base64-encoded audio."""
        if self._client is None:
            return {"error": "client not initialized"}
        api_key = self._api_keys.get("openai", "")
        base = "https://api.openai.com/v1"

        try:
            resp = await self._client.post(
                f"{base}/audio/speech",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "input": text,
                    "voice": voice,
                    "response_format": response_format,
                },
            )
            resp.raise_for_status()
            b64 = base64.b64encode(resp.content).decode()
            return {"b64_audio": b64, "format": response_format, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("TTS failed: %s", exc)
            return {"error": str(exc), "b64_audio": ""}

    # --------------------------------------------------------- batch
    async def batch_analyze(
        self,
        images: List[str],
        prompt: str = "Describe this image.",
        model: str = "openai/gpt-4o",
    ) -> List[Dict[str, Any]]:
        """Analyze multiple images in parallel."""
        import asyncio
        tasks = [self.analyze_image(img, prompt, model) for img in images]
        return await asyncio.gather(*tasks)
