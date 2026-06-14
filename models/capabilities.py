"""Model capability detection.

Given a ``ModelInfo`` (id, name, features, modalities, context length) we
classify what the model can do, so the user can ask "which model is best
for vision / video / code / agent?" without having to read every
provider's documentation.

The detection is intentionally **name-first** — every modern LLM API
exposes ``GET /v1/models`` with varying levels of metadata, but the model
``id`` is the only field that's always present.  We fall back to
``features`` and ``input_modalities`` when available, then to a
generous regex over the id/name as a last resort.

The detected capabilities are stored on ``ModelInfo.capabilities`` as a
``frozenset[str]``.  Use ``detect_capabilities()`` to (re-)compute it.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)


# ============================================================
# Capability tags
# ============================================================
CAP_TEXT            = "text"             # general chat / instruction following
CAP_VISION          = "vision"           # image input (image -> text)
CAP_IMAGE_GEN       = "image_generation" # image output (text -> image)
CAP_VIDEO           = "video"            # video generation / understanding
CAP_AUDIO_IN        = "audio_in"         # speech-to-text (Whisper, ASR)
CAP_AUDIO_OUT       = "audio_out"        # text-to-speech (TTS, voice)
CAP_EMBEDDINGS      = "embeddings"       # text-embedding-*, e5-*, bge-*
CAP_CODE            = "code"             # code-specialised (codellama, deepseek-coder)
CAP_TOOLS           = "tools"            # function calling / agent use
CAP_REASONING       = "reasoning"        # chain-of-thought, thinking tokens
CAP_JSON_MODE       = "json_mode"        # structured output
CAP_STREAMING       = "streaming"        # SSE
CAP_LONG_CONTEXT    = "long_context"     # context length >= 100K
CAP_MULTILINGUAL    = "multilingual"     # strong non-English support
CAP_FINE_TUNE       = "fine_tune"        # supports fine-tuning

ALL_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_TEXT, CAP_VISION, CAP_IMAGE_GEN, CAP_VIDEO,
    CAP_AUDIO_IN, CAP_AUDIO_OUT, CAP_EMBEDDINGS, CAP_CODE,
    CAP_TOOLS, CAP_REASONING, CAP_JSON_MODE, CAP_STREAMING,
    CAP_LONG_CONTEXT, CAP_MULTILINGUAL, CAP_FINE_TUNE,
})


# ============================================================
# Name-based hint regexes
# ============================================================
# We compile a single master regex with named groups so a single pass
# gives us all signals.  Order matters: more specific tokens come first
# so a model like "gpt-4o-mini-tts" matches "tts" not "gpt-4o".
_NAME_PATTERNS: List[tuple] = [
    # --- specialised non-chat models (return early) ---
    (CAP_EMBEDDINGS, re.compile(
        r"(text-?embedding|embeddings?\b|embeds?\b|^bge[-/]|"
        r"^e5[-/]|^gte[-/]|^m3e|^b1-?embedding|^qwen.*embedding|"
        r"cohere-?embed|embed-?english|embed-?multilingual|nomic-?embed)",
        re.IGNORECASE,
    )),
    (CAP_IMAGE_GEN, re.compile(
        r"(dall-?e|stable-?diffusion|sdxl|sd-?3|sd-?xl|flux(\.|dev|schnell)?|"
        r"midjourney|imagen[\d-]*|kandinsky|recraft|krea|ideogram|"
        r"cogview|wanx(\b|-)|kolors|firefly|nova-?canv|playground-v2|"
        r"image-?alpha|imagegen|image-gen|pixart)",
        re.IGNORECASE,
    )),
    (CAP_VIDEO, re.compile(
        r"(sora(\b|-)|veo(\b|-)|kling(\b|-)|runway(\b|-|ml)|gen-?2|"
        r"gen-?3|pika(\b|-)|luma(\b|-)|cogvideo|haijiao|minimax-?video|"
        r"hailuo(\b|-)|hunyuan-?video|wan-?video|wan2|"
        r"video-?01|video-?gen|kuaishou-?keling)",
        re.IGNORECASE,
    )),
    (CAP_AUDIO_IN, re.compile(
        r"(whisper|asr|speech-?to-?text|^paraformer|^sensevoice|"
        r"audio-?input|^stt-|\baudio\b.*input)",
        re.IGNORECASE,
    )),
    (CAP_AUDIO_OUT, re.compile(
        r"(^tts[-/]|^text-?to-?speech|elevenlabs|^speech-|"
        r"speech-?\d|^openai-?tts|^azure-?tts|^google-?tts|"
        r"^xtts|^bark|^cosyvoice|minimax-?speech|hailuo-?tts|"
        r"tts-?1\b|tts-?1-?hd)",
        re.IGNORECASE,
    )),
    # --- chat models with specific specialities ---
    (CAP_REASONING, re.compile(
        r"(^|\b|-)(o1[-/]?preview|o1[-/]?mini|o1\b|o3[-/]?mini|o3\b|o4\b|"
        r"r1\b|qwq|skywork-?o1|deepseek-?r1|deepseek.?reasoner|"
        r"thinking|reasoning|gemini-?2\.5|claude-?3\.7|claude-?4|"
        r"sonnet-?4|kimi-?k1|kimi-?thinking|minimax-?thinking|"
        r"qwen-?qwq|qwen3-?thinking|hunyuan-?thinking)\b",
        re.IGNORECASE,
    )),
    (CAP_CODE, re.compile(
        r"(code(\.|-|_)|coder|starcoder|codegeex|codeium|codestral|"
        r"deepseek-?coder|deepseek-?v2\.5|qwen2\.5-?coder|qwen-?coder|"
        r"codellama|llama-?code|codestral-?22|codestral-?25|"
        r"wizardcoder|codebooga|granite-?code|magicoder|"
        r"yi-?coder|baichuan-?code|codefuse)",
        re.IGNORECASE,
    )),
    (CAP_VISION, re.compile(
        r"(\bvision\b|gpt-?4(\.|o|opus|o-?preview)|gpt-?4\.1|"
        r"claude-?3|claude-?4|gemini-?[12]|"
        r"qwen-?vl|qwen2-?vl|qvq|qwen-?2-?vl|internvl|llava|cogvlm|cogagent|"
        r"kimi-?vl|minicpm-?v|yi-?vl|deepseek-?vl|"
        r"pixtral|\bvl-?7b|\bvl-?13b|\bvl-?34b|"
        r"nova-?lite|nova-?pro|nova-?premier|reka-?(core|edge|flash)|"
        r"molmo|phi-?3\.5-?vision|phi-?4)",
        re.IGNORECASE,
    )),
    (CAP_TOOLS, re.compile(
        r"(function-?calling|tool-?use|^tools-?|agent-?\d|agent-?mode|"
        r"function-?call|gpt-?4|claude-?3|claude-?4|gemini-?[12]|"
        r"qwen-?max|qwen-?plus|qwen2\.5-?72b|qwen-?2\.5-?72b|"
        r"deepseek-?v3|deepseek-?chat|yi-?large|kimi-?k2|"
        r"minimax-?abab|baichuan-?4|glm-?4|chatglm-?4|"
        r"mistral-?large|mistral-?small|llama-?3\.1-?70|llama-?3\.1-?405|"
        r"command-?r|command-?r-?plus|nova-?pro|nova-?lite)",
        re.IGNORECASE,
    )),
    (CAP_LONG_CONTEXT, re.compile(
        r"(gemini-?1\.5|gemini-?2|kimi-?k2|yi-?lightning|"
        r"qwen2\.5-?1m|minimax-?abab-?6\.5s|"
        r"long-?llama|chatglm3-?6b-?32k|yi-?34b-?200k|"
        r"claude-?2\.1|claude-?3-?200k|claude-?4)",
        re.IGNORECASE,
    )),
    (CAP_MULTILINGUAL, re.compile(
        r"(qwen|baichuan|chatglm|yi-?|sensenova|hunyuan|ernie|"
        r"spark|kimi-?k|minimax-?abab|yi-?1\.5|qwen2)",
        re.IGNORECASE,
    )),
]


# Tags that indicate a model is "chat capable" (and therefore should get
# the default ``text`` capability when other signals are absent).
_CHAT_NAME_HINTS = re.compile(
    r"(chat|instruct|it\b|base\b|^gpt|^claude|^gemini|^llama|^mistral|"
    r"^qwen|^deepseek|^yi|^kimi|^baichuan|^minimax|^ernie|^spark|"
    r"^command|^nova|^reka|^phi|^falcon|^mixtral|^cohere|^grok|^gemma|"
    r"^dbrx|^jamba|^c4ai-|^cog-|^nous-?hermes|^openchat|"
    r"sensenova-|doubao-?pro|doubao-?lite|^step-|hunyuan-?pro|"
    r"abab-|chatglm|glm-?4)",
    re.IGNORECASE,
)


def detect_capabilities(model: "ModelInfo") -> Set[str]:  # noqa: F821
    """Compute the capability set for a single model.

    The result is intentionally a ``set`` (not ``frozenset``) so the
    caller can mutate it; ``ModelCatalog._normalize`` converts it to a
    ``frozenset`` before storing it on ``ModelInfo.capabilities``.
    """
    name = (model.id or "") + " " + (model.name or "")
    name_lc = name.lower()
    feats = {f.lower() for f in (model.features or [])}
    in_mod = {m.lower() for m in (model.input_modalities or [])}
    out_mod = {m.lower() for m in (model.output_modalities or [])}

    caps: Set[str] = set()

    # ---- metadata-driven signals (highest priority) ----
    if "vision" in feats or "image" in in_mod:
        caps.add(CAP_VISION)
    if any(x in feats for x in ("tools", "function_calling")) or "tools" in name_lc:
        caps.add(CAP_TOOLS)
    if "reasoning" in feats or "thinking" in feats:
        caps.add(CAP_REASONING)
    if "json_mode" in feats or "json" in feats:
        caps.add(CAP_JSON_MODE)
    if "streaming" in feats:
        caps.add(CAP_STREAMING)
    if "audio" in in_mod and "audio" not in out_mod:
        caps.add(CAP_AUDIO_IN)
    if "audio" in out_mod:
        caps.add(CAP_AUDIO_OUT)

    # ---- long-context signal (metadata or hint) ----
    ctx = int(model.context_length or 0)
    if ctx >= 100_000:
        caps.add(CAP_LONG_CONTEXT)

    # ---- name-driven signals (regex sweep) ----
    early_exit: Optional[str] = None
    for tag, regex in _NAME_PATTERNS:
        if regex.search(name):
            caps.add(tag)
            # Embeddings / image-gen / video / audio-only models
            # should NOT also be marked as generic chat models.
            if tag in (CAP_EMBEDDINGS, CAP_IMAGE_GEN, CAP_VIDEO, CAP_AUDIO_IN, CAP_AUDIO_OUT):
                early_exit = tag
    if early_exit is not None:
        # Specialised non-chat model — return what we have, no "text"
        return caps

    # ---- default: chat model with "text" capability ----
    if _CHAT_NAME_HINTS.search(name) or not caps:
        caps.add(CAP_TEXT)

    return caps


# ============================================================
# Recommendation engine
# ============================================================
# Each category is a list of capabilities the model must have to qualify.
# We also keep a default "best for X" so the recommendation stays useful
# even if a provider has only one or two models.
RECOMMEND_CATEGORIES: Dict[str, Dict[str, Any]] = {
    "best_paid":        {"label": "最佳付费",   "require": (),              "prefer": "paid"},
    "best_free":        {"label": "最佳免费",   "require": (),              "prefer": "free"},
    "best_for_text":    {"label": "最佳文本",   "require": (CAP_TEXT,)},
    "best_for_vision":  {"label": "最佳视觉",   "require": (CAP_VISION,)},
    "best_for_image":   {"label": "最佳图像生成", "require": (CAP_IMAGE_GEN,)},
    "best_for_video":   {"label": "最佳视频",   "require": (CAP_VIDEO,)},
    "best_for_audio":   {"label": "最佳音频",   "require": (CAP_AUDIO_IN, CAP_AUDIO_OUT)},
    "best_for_code":    {"label": "最佳代码",   "require": (CAP_CODE,)},
    "best_for_agent":   {"label": "最佳 Agent", "require": (CAP_TOOLS,)},
    "best_for_reasoning": {"label": "最佳推理", "require": (CAP_REASONING,)},
    "best_for_long_context": {"label": "最佳长上下文", "require": (CAP_LONG_CONTEXT,)},
    "best_for_embeddings": {"label": "最佳 Embedding", "require": (CAP_EMBEDDINGS,)},
}


def _score_model(m: "ModelInfo", category: str) -> float:  # noqa: F821
    """Heuristic score for ranking candidate models in a category.

    Higher = better.  Used to pick the top model per category.
    """
    s = 0.0
    cfg = RECOMMEND_CATEGORIES.get(category, {})

    # Free vs paid preference
    prefer = cfg.get("prefer")
    if prefer == "paid" and not m.is_free:
        s += 100
    elif prefer == "free" and m.is_free:
        s += 100

    # Tier bonus (expert > complex > simple > trivial)
    s += {"trivial": 0, "simple": 5, "complex": 10, "expert": 20}.get(m.tier, 0)

    # Context length (more = better, capped)
    ctx = int(m.context_length or 0)
    s += min(ctx / 10_000.0, 50.0)

    # Feature richness
    s += len(m.features or []) * 3.0

    # Reasoning models get a small bonus in most categories
    caps = m.capabilities or set()
    if CAP_REASONING in caps:
        s += 5.0
    if CAP_TOOLS in caps:
        s += 3.0

    return s


def recommend(models: Iterable["ModelInfo"]) -> Dict[str, Optional["ModelInfo"]]:  # noqa: F821
    """Pick the best model per category from ``models``.

    Returns a dict keyed by category name (see :data:`RECOMMEND_CATEGORIES`).
    Categories with no qualifying model get ``None``.
    """
    models = list(models)
    out: Dict[str, Optional[Any]] = {cat: None for cat in RECOMMEND_CATEGORIES}
    if not models:
        return out

    for cat, cfg in RECOMMEND_CATEGORIES.items():
        require = cfg.get("require") or ()
        candidates = []
        for m in models:
            caps = m.capabilities or set()
            if require and not all(r in caps for r in require):
                continue
            candidates.append(m)
        if not candidates:
            continue
        # Single candidate wins by default; otherwise rank by score
        best = max(candidates, key=lambda m: _score_model(m, cat))
        out[cat] = best
    return out


# ============================================================
# Human-readable description (for CLI / chat replies)
# ============================================================
_CAPABILITY_LABELS: Dict[str, str] = {
    CAP_TEXT: "文本", CAP_VISION: "视觉输入", CAP_IMAGE_GEN: "图像生成",
    CAP_VIDEO: "视频", CAP_AUDIO_IN: "语音识别", CAP_AUDIO_OUT: "语音合成",
    CAP_EMBEDDINGS: "Embedding", CAP_CODE: "代码", CAP_TOOLS: "工具/Agent",
    CAP_REASONING: "推理", CAP_JSON_MODE: "JSON", CAP_STREAMING: "流式",
    CAP_LONG_CONTEXT: "长上下文", CAP_MULTILINGUAL: "多语种",
    CAP_FINE_TUNE: "可微调",
}


def describe_capabilities(caps: Iterable[str]) -> str:
    """Return a short Chinese/English label list like ``'文本|视觉|代码'``."""
    if not caps:
        return "(未识别)"
    labels = []
    for c in caps:
        labels.append(_CAPABILITY_LABELS.get(c, c))
    return "|".join(labels)
