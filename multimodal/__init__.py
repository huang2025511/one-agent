"""Multimodal support — image generation, image understanding, and TTS.

Provides:
  - Image generation (OpenAI DALL-E / Stability AI via OpenRouter)
  - Image understanding (GPT-4V / Claude Vision via vision-capable models)
  - Text-to-speech (OpenAI TTS / ElevenLabs)
  - Base64-encoded image handling end-to-end

Architectural notes (v2.1):
  - Provider base URLs come from ``models.resolver`` — no local hardcoding
    so sensenova / zhipu / moonshot / any new provider works automatically.
  - One ``httpx.AsyncClient`` per provider (with ``base_url=``) for
    connection-pool reuse across calls.
  - Unknown providers raise ``ValueError`` instead of silently routing to
    OpenAI (which would 401 and confuse the user).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# Endpoint families — different OpenAI-compatible providers expose
# different endpoint paths for the same logical operation.
_ENDPOINT_FOR_TASK = {
    "image_gen":   "/images/generations",
    "vision":      "/chat/completions",
    "tts":         "/audio/speech",
}


class MultimodalPlugin(Plugin):
    """Handles image generation, vision, and TTS."""

    name = "multimodal"

    def __init__(self) -> None:
        super().__init__()
        self._api_keys: Dict[str, str] = {}
        self._timeout = 60
        # Per-provider httpx clients (lazily created so disabled providers
        # don't open sockets).  Keyed by provider name.
        self._clients: Dict[str, httpx.AsyncClient] = {}
        # Provider → base URL (populated from resolver at setup)
        self._base_urls: Dict[str, str] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        llm_cfg = ctx.config.get("llm", {}) or {}
        self._api_keys = llm_cfg.get("api_keys", {}) or {}
        self._timeout = int(llm_cfg.get("timeout", 60))
        # Pull the full provider registry so we recognise sensenova/zhipu/etc.
        try:
            from models.resolver import list_known
            self._base_urls.update(list_known())
        except Exception:  # noqa: BLE001
            # Fallback to a tiny built-in table
            self._base_urls = {
                "openai": "https://api.openai.com/v1",
                "openrouter": "https://openrouter.ai/api/v1",
                "anthropic": "https://api.anthropic.com/v1",
            }
        # Config-level overrides win
        custom = llm_cfg.get("base_urls", {}) or {}
        self._base_urls.update(custom)
        logger.info("multimodal ready, providers=%d", len(self._base_urls))

    async def stop(self) -> None:
        for cli in self._clients.values():
            try:
                await cli.aclose()
            except Exception:
                pass
        self._clients.clear()
        await super().stop()

    # ------------------------------------------------------- provider dispatch
    def _resolve(self, model: str):
        """Return (provider, model_name, api_key, base_url, client).

        Raises ``ValueError`` for unknown providers instead of silently
        falling back to OpenAI.
        """
        provider, model_name = model.split("/", 1) if "/" in model else ("openai", model)
        api_key = (
            self._api_keys.get(provider)
            or self._api_keys.get("openrouter", "")
            or self._api_keys.get("openai", "")
        )
        base = self._base_urls.get(provider)
        if not base:
            raise ValueError(
                f"unsupported provider '{provider}' for multimodal — "
                f"set llm.base_urls.{provider} or use one of "
                f"{sorted(self._base_urls.keys())[:5]}…"
            )
        if not api_key or "${" in api_key:
            raise ValueError(f"no API key configured for provider '{provider}'")
        return provider, model_name, api_key, base, self._client_for(base)

    def _client_for(self, base_url: str) -> httpx.AsyncClient:
        """One pooled client per base_url.  Keeps the per-provider
        connection pool warm for subsequent calls."""
        cli = self._clients.get(base_url)
        if cli is None:
            cli = httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
            self._clients[base_url] = cli
        return cli

    # --------------------------------------------------------- image generation
    async def generate_image(
        self,
        prompt: str,
        model: str = "openai/dall-e-3",
        size: str = "1024x1024",
        quality: str = "standard",
        n: int = 1,
    ) -> Dict[str, Any]:
        """Generate images from a text prompt.  Returns base64 image data."""
        try:
            _provider, model_name, api_key, _base, cli = self._resolve(model)
        except ValueError as exc:
            return {"error": str(exc), "images": []}

        try:
            resp = await cli.post(
                _ENDPOINT_FOR_TASK["image_gen"],
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
                images.append({
                    "b64_json": item.get("b64_json", ""),
                    "revised_prompt": item.get("revised_prompt", ""),
                })
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
        """Analyze an image (base64, URL, or local file path)."""
        try:
            _provider, model_name, api_key, _base, cli = self._resolve(model)
        except ValueError as exc:
            return {"error": str(exc), "analysis": ""}

        # Detect format: URL, data URI, file path, or raw base64
        if image_data.startswith("http://") or image_data.startswith("https://"):
            image_payload: Dict[str, Any] = {"url": image_data}
        elif image_data.startswith("data:"):
            image_payload = {"url": image_data}
        elif image_data.startswith("/") or image_data.startswith("./"):
            # Local file path → base64 with correct MIME type
            # Security: validate path to prevent directory traversal
            try:
                p = Path(image_data).resolve()
                # Only allow files in current directory or subdirectories
                # Use relative_to() for strict containment (startswith can be bypassed)
                cwd = Path.cwd().resolve()
                try:
                    p.relative_to(cwd)
                except ValueError:
                    return {"error": "access denied: path outside working directory", "analysis": ""}
                # Validate file extension
                allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
                if p.suffix.lower() not in allowed_ext:
                    return {"error": f"invalid file type: {p.suffix}", "analysis": ""}
                if not p.exists() or not p.is_file():
                    return {"error": "file not found or not a regular file", "analysis": ""}
                mime, _ = mimetypes.guess_type(str(p))
                mime = mime or "image/png"
                b64 = base64.b64encode(p.read_bytes()).decode()
                image_payload = {"url": f"data:{mime};base64,{b64}"}
            except (OSError, ValueError) as exc:
                return {"error": f"invalid path: {exc}", "analysis": ""}
        else:
            # Assume raw base64 (PNG default for backwards-compat)
            image_payload = {"url": f"data:image/png;base64,{image_data}"}

        try:
            resp = await cli.post(
                _ENDPOINT_FOR_TASK["vision"],
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
        try:
            _provider, model_name, api_key, _base, cli = self._resolve(model)
        except ValueError as exc:
            return {"error": str(exc), "b64_audio": ""}
        try:
            resp = await cli.post(
                _ENDPOINT_FOR_TASK["tts"],
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    # Use the un-prefixed model name — OpenAI-compatible
                    # TTS endpoints expect "tts-1", not "openai/tts-1".
                    "model": model_name,
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

    # --------------------------------------------------------- audio transcription
    async def transcribe_audio(
        self,
        audio_path: str,
        language: str = "zh",
    ) -> Dict[str, Any]:
        """Transcribe audio to text using Whisper API.

        Tries the local ``whisper`` CLI first (openai-whisper), then falls
        back to the OpenAI-compatible whisper-1 model via the LLM provider.
        """
        import subprocess

        if not os.path.exists(audio_path):
            return {"text": "", "error": f"audio file not found: {audio_path}"}

        # Security: validate path to prevent directory traversal
        try:
            p = Path(audio_path).resolve()
            cwd = Path.cwd().resolve()
            if not str(p).startswith(str(cwd)):
                return {"text": "", "error": "access denied: path outside working directory"}
            # Validate file extension
            allowed_ext = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
            if p.suffix.lower() not in allowed_ext:
                return {"text": "", "error": f"invalid file type: {p.suffix}"}
            if not p.exists() or not p.is_file():
                return {"text": "", "error": "file not found or not a regular file"}
            audio_path = str(p)
        except (OSError, ValueError) as exc:
            return {"text": "", "error": f"invalid path: {exc}"}

        # Try local whisper CLI first (fast, offline)
        try:
            result = subprocess.run(
                ["whisper", audio_path, "--language", language, "--model", "tiny",
                 "--output_format", "txt", "--output_dir", "/tmp"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                out_path = os.path.splitext(audio_path)[0] + ".txt"
                if os.path.exists(out_path):
                    with open(out_path, encoding="utf-8") as f:
                        text = f.read().strip()
                    return {"text": text, "method": "local_whisper"}
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        except Exception as exc:
            logger.debug("local whisper failed: %s", exc)

        # Fallback: use OpenAI-compatible whisper-1 endpoint
        try:
            _provider, _model_name, api_key, _base, cli = self._resolve("openai/whisper-1")
        except ValueError as exc:
            return {"text": "", "error": str(exc)}

        try:
            with open(audio_path, "rb") as f:
                resp = await cli.post(
                    "/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": "whisper-1", "language": language},
                    files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                )
            resp.raise_for_status()
            data = resp.json()
            return {"text": data.get("text", ""), "method": "openai_whisper"}
        except Exception as exc:
            logger.warning("whisper API transcription failed: %s", exc)
            return {"text": "", "error": str(exc)}

    # --------------------------------------------------------- image description (convenience)
    async def describe_image(
        self,
        image_path: str,
        question: str = "",
    ) -> Dict[str, Any]:
        """Describe an image using a vision-capable LLM.

        Reads the image, base64-encodes it, and returns a structured payload
        that the coordinator can forward to a vision model via
        :meth:`LLMProvider.chat_completion`.
        """
        import base64 as _b64
        from pathlib import Path

        path = Path(image_path).resolve()
        # Security: validate path to prevent directory traversal
        cwd = Path.cwd().resolve()
        if not str(path).startswith(str(cwd)):
            return {"error": "access denied: path outside working directory", "image_base64": "", "mime_type": "", "prompt": ""}
        allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        if path.suffix.lower() not in allowed_ext:
            return {"error": f"invalid file type: {path.suffix}", "image_base64": "", "mime_type": "", "prompt": ""}
        if not path.exists() or not path.is_file():
            return {"error": "image not found", "image_base64": "", "mime_type": "", "prompt": ""}

        ext = path.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        }
        mime_type = mime_map.get(ext, "image/png")

        image_data = _b64.b64encode(path.read_bytes()).decode()

        prompt = question or "请描述这张图片的内容"

        return {
            "image_base64": image_data,
            "mime_type": mime_type,
            "prompt": prompt,
            "model_hint": "vision",
        }

    # --------------------------------------------------------- batch
    async def batch_analyze(
        self,
        images: List[str],
        prompt: str = "Describe this image.",
        model: str = "openai/gpt-4o",
    ) -> List[Dict[str, Any]]:
        """Analyze multiple images in parallel."""
        tasks = [self.analyze_image(img, prompt, model) for img in images]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Convert any exceptions to error dicts so callers can safely .get()
        normalized: List[Dict[str, Any]] = []
        for r in results:
            if isinstance(r, Exception):
                normalized.append({"error": str(r), "analysis": ""})
            else:
                normalized.append(r)
        return normalized

    # --------------------------------------------------------- OCR
    async def ocr_image(
        self,
        image_path: str,
        language: str = "chi_sim+eng",
        model: str = "openai/gpt-4o",
    ) -> Dict[str, Any]:
        """对图片进行 OCR 文字识别。

        优先使用本地 pytesseract（离线、快速），不可用时回退到
        视觉大模型（GPT-4o / Claude Vision）进行文字提取。

        Args:
            image_path: 本地图片路径
            language: tesseract 语言代码（默认中文+英文）
            model: 回退视觉模型

        Returns:
            {"text": "识别到的文字", "method": "tesseract"|"vision", "confidence": float}
        """
        # 安全校验：防止目录遍历
        try:
            p = Path(image_path).resolve()
            cwd = Path.cwd().resolve()
            try:
                p.relative_to(cwd)
            except ValueError:
                return {"text": "", "error": "access denied: path outside working directory", "method": ""}
            allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
            if p.suffix.lower() not in allowed_ext:
                return {"text": "", "error": f"invalid file type: {p.suffix}", "method": ""}
            if not p.exists() or not p.is_file():
                return {"text": "", "error": "file not found or not a regular file", "method": ""}
        except (OSError, ValueError) as exc:
            return {"text": "", "error": f"invalid path: {exc}", "method": ""}

        # 方案一：本地 pytesseract（离线）
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore

            img = Image.open(str(p))
            text = pytesseract.image_to_string(img, lang=language)
            text = text.strip()
            if text:
                return {"text": text, "method": "tesseract", "confidence": 0.9}
        except ImportError:
            logger.debug("pytesseract/PIL 未安装，回退到视觉模型 OCR")
        except Exception as exc:
            logger.debug("pytesseract OCR 失败，回退到视觉模型: %s", exc)

        # 方案二：视觉大模型回退
        try:
            result = await self.analyze_image(
                str(p),
                prompt="请提取这张图片中的所有文字内容，保持原始格式和排版。只输出识别到的文字，不要添加解释。",
                model=model,
            )
            if result.get("error"):
                return {"text": "", "error": result["error"], "method": ""}
            return {"text": result.get("analysis", ""), "method": "vision", "confidence": 0.8}
        except Exception as exc:
            logger.warning("vision OCR failed: %s", exc)
            return {"text": "", "error": str(exc), "method": ""}


# ------------------------------------------------------------------ skill handler factories

def make_transcribe_handler():
    """Return a handler that transcribes audio files to text using Whisper."""

    async def handler(args):
        path = args.get("path", args.get("input", ""))
        if not path:
            return "请提供音频文件路径（path 或 input 参数）"
        import os
        from pathlib import Path

        # Security: validate path to prevent directory traversal
        try:
            p = Path(path).resolve()
            cwd = Path.cwd().resolve()
            if not str(p).startswith(str(cwd)):
                return "access denied: path outside working directory"
            allowed_ext = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
            if p.suffix.lower() not in allowed_ext:
                return f"invalid file type: {p.suffix}"
            if not p.exists() or not p.is_file():
                return "file not found or not a regular file"
            path = str(p)
        except (OSError, ValueError) as exc:
            return f"invalid path: {exc}"

        try:
            result = subprocess.run(
                ["whisper", path, "--language", "zh", "--model", "tiny",
                 "--output_format", "txt", "--output_dir", "/tmp"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                out_path = os.path.splitext(path)[0] + ".txt"
                if os.path.exists(out_path):
                    with open(out_path, encoding="utf-8") as f:
                        return f.read().strip()
            return f"[whisper 失败: {result.stderr[:200]}]"
        except FileNotFoundError:
            return "[whisper 未安装。请运行: pip install openai-whisper]"
        except subprocess.TimeoutExpired:
            return "[whisper 超时：音频文件可能过大]"
        except Exception as exc:
            return f"[转录失败: {exc}]"

    return handler


def make_image_handler():
    """Return a handler that encodes an image for vision model analysis."""

    async def handler(args):
        path = args.get("path", args.get("input", ""))
        question = args.get("question", "请描述这张图片")
        if not path:
            return "请提供图片路径（path 或 input 参数）"
        import base64
        from pathlib import Path

        # Security: validate path to prevent directory traversal
        try:
            p = Path(path).resolve()
            cwd = Path.cwd().resolve()
            if not str(p).startswith(str(cwd)):
                return "access denied: path outside working directory"
            allowed_ext = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
            if p.suffix.lower() not in allowed_ext:
                return f"invalid file type: {p.suffix}"
            if not p.exists() or not p.is_file():
                return "file not found or not a regular file"
            path = str(p)
        except (OSError, ValueError) as exc:
            return f"invalid path: {exc}"

        ext = os.path.splitext(path)[1].lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        }
        mime = mime_map.get(ext, "image/png")

        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        return json.dumps({
            "type": "image_request",
            "image_base64": b64,
            "mime_type": mime,
            "question": question,
            "hint": "需要使用支持视觉的模型（如 GPT-4o、Claude 3 Vision）来回答",
        }, ensure_ascii=False)

    return handler
