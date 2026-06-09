"""多张候选图选最佳（改造 C-3）。

每个分镜生成 N 张候选图后，让多模态 LLM 对比+选最匹配的一张。
- 复用 `app.services.llm_multimodal.openai_vision_tag_caller` 通道。
- 失败时降级：返回第一张（保证流程不挂）。
- 注入式 caller，便于测试 mock。
"""

from __future__ import annotations

import json
import re
from typing import Callable, List, Optional

from loguru import logger

from app.services.ai_image import GeneratedImage


SCORING_SYSTEM_PROMPT = """
# Role: Visual Consistency Filter

## Goal
Given N candidate images and a target visual description, pick the SINGLE
image that best matches the description.

## Output Format
Return a JSON object: {"best_index": <int 0-based>, "reason": "<short>"}
- best_index ∈ [0, N-1]
- Do NOT wrap in markdown fences.

## Selection Criteria
1. Subject and action match the description verbatim.
2. Setting / lighting / style match.
3. Prefer images without text overlays, watermarks, or distortion.
""".strip()


_INDEX_RE = re.compile(r'"best_index"\s*:\s*(\d+)')


def render_user_prompt(
    visual_desc: str, candidate_count: int, image_paths: List[str]
) -> str:
    return (
        f"Target visual description:\n\"\"\"\n{visual_desc}\n\"\"\"\n\n"
        f"Number of candidates: {candidate_count}\n"
        f"Image paths (for caller to attach as image inputs in order): {image_paths}\n\n"
        "Pick the index that best matches. Respond with JSON only."
    )


def parse_best_index(raw_response: str, n_candidates: int) -> int:
    """解析 LLM 返回的 best_index。失败 / 越界返 0。"""
    if not raw_response:
        return 0
    # 优先 JSON
    try:
        data = json.loads(raw_response.strip())
        if isinstance(data, dict) and "best_index" in data:
            idx = int(data["best_index"])
            if 0 <= idx < n_candidates:
                return idx
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # 兜底正则
    m = _INDEX_RE.search(raw_response)
    if m:
        idx = int(m.group(1))
        if 0 <= idx < n_candidates:
            return idx
    return 0


VLMCaller = Callable[[str, str, List[str]], str]
"""(system_prompt, user_prompt, image_paths) -> raw_response

签名跟 auto_tagger.TagCaller 一致；可以直接复用 llm_multimodal 的 caller。
"""


def select_best_image(
    candidates: List[GeneratedImage],
    visual_desc: str,
    vlm_caller: Optional[VLMCaller] = None,
) -> GeneratedImage:
    """从候选图中选一张最匹配的。

    - candidates: 必须非空，至少一张；只有一张时直接返回。
    - vlm_caller: 多模态 LLM 调用器；为 None 时返回第一张。
    - 任何调用失败一律降级为返回第一张，不抛错。
    """
    if not candidates:
        raise ValueError("candidates cannot be empty")
    if len(candidates) == 1 or vlm_caller is None:
        return candidates[0]

    paths = [c.local_path for c in candidates]
    user_prompt = render_user_prompt(visual_desc, len(candidates), paths)
    try:
        raw = vlm_caller(SCORING_SYSTEM_PROMPT, user_prompt, paths)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "consistency_filter: VLM call failed, defaulting to first image: {}",
            exc,
        )
        return candidates[0]
    idx = parse_best_index(raw, n_candidates=len(candidates))
    logger.info(
        "consistency_filter: picked candidate #{} of {}", idx, len(candidates)
    )
    return candidates[idx]


__all__ = [
    "SCORING_SYSTEM_PROMPT",
    "parse_best_index",
    "render_user_prompt",
    "select_best_image",
]
