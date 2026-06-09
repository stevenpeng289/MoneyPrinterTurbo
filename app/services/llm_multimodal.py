"""多模态 LLM 调用器（改造 D 辅助）。

设计目标：
- 为 `auto_tagger.TagCaller` 提供一个可即用的默认实现，调 OpenAI 兼容 vision API。
- MiniMax / OpenAI / 月之暗面等 OpenAI-compatible vision provider 都用同一份代码。
- Gemini 的 image inline 格式不同，本模块暂用 OpenAI 协议为基线；
  Gemini 用户可以填 `gemini_*_via_openai_proxy` 走 OpenAI 兼容路径，
  或后续单独扩展。

注意：
- 帧文件本地读 → base64 → data URL 注入 `image_url`，避免依赖外网托管。
- 失败时降级：用纯文本通道（让 LLM 根据 filename 猜 tags），保证流程能跑通。
"""

from __future__ import annotations

import base64
import os
from typing import List

from loguru import logger
from openai import OpenAI

from app.config import config


def _resolve_provider() -> str:
    """读取配置的 multimodal provider，默认跟 llm_provider 一致。"""
    return (
        config.app.get("auto_tagger_provider", "").strip()
        or config.app.get("llm_provider", "openai").strip()
    )


def _resolve_openai_compatible_client(provider: str) -> tuple[OpenAI, str]:
    """返回 (client, model_name)。

    支持 openai / aihubmix / moonshot / oneapi / minimax / mimo / deepseek / grok /
    groq / modelscope / pollinations / litellm-via-openai 等 OpenAI 兼容 provider。
    """
    api_key = config.app.get(f"{provider}_api_key", "")
    base_url = config.app.get(f"{provider}_base_url", "") or None
    model_name = config.app.get(f"{provider}_model_name", "")
    if not api_key:
        raise RuntimeError(
            f"multimodal provider '{provider}' missing api key (config: {provider}_api_key)"
        )
    return OpenAI(api_key=api_key, base_url=base_url), model_name


def _encode_image_data_url(path: str) -> str:
    with open(path, "rb") as f:
        b = f.read()
    return "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")


def openai_vision_tag_caller(
    system_prompt: str, user_prompt: str, frame_paths: List[str]
) -> str:
    """OpenAI 兼容 vision API caller。

    把帧 base64 inline 到 message 里，避免外网托管依赖。
    """
    provider = _resolve_provider()
    client, model = _resolve_openai_compatible_client(provider)

    content: list[dict] = [{"type": "text", "text": user_prompt}]
    for fp in frame_paths:
        if not os.path.isfile(fp):
            logger.warning(f"multimodal: frame missing, skipping: {fp}")
            continue
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _encode_image_data_url(fp)},
            }
        )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    )
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError(f"multimodal: empty choices from {provider}/{model}")
    message = getattr(choices[0], "message", None)
    if message is None or not getattr(message, "content", None):
        raise RuntimeError(f"multimodal: empty message from {provider}/{model}")
    return str(message.content)


def filename_fallback_tag_caller(
    system_prompt: str, user_prompt: str, frame_paths: List[str]
) -> str:
    """降级：纯文本通道，让 LLM 根据 filename 猜 tags。

    用法：当 multimodal API 不可用 / 出错时切到这个 caller，保证流程能跑通。
    """
    from app.services import llm

    combined = (
        f"{system_prompt}\n\n---\n\n"
        f"{user_prompt}\n\n"
        f"NOTE: Visual frames are not available in this fallback mode. "
        f"Infer tags ONLY from the filename and any hints in the user prompt."
    )
    return llm._generate_response(prompt=combined)  # noqa: SLF001


def get_default_tag_caller():
    """工厂：返回一个 caller。

    优先用 multimodal vision（OpenAI 兼容协议）；
    如果配置不完整（缺 api_key 等），返回降级 caller。
    """
    try:
        provider = _resolve_provider()
        api_key = config.app.get(f"{provider}_api_key", "")
        if not api_key:
            logger.warning(
                "multimodal provider '{}' missing api_key, falling back to filename mode",
                provider,
            )
            return filename_fallback_tag_caller
        return openai_vision_tag_caller
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"multimodal default caller setup failed: {exc}, using fallback")
        return filename_fallback_tag_caller


__all__ = [
    "filename_fallback_tag_caller",
    "get_default_tag_caller",
    "openai_vision_tag_caller",
]
