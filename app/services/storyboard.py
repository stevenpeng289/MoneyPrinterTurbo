"""脚本 → 分镜拆分服务（改造 C-2）。

把已经写好的视频脚本（generate_script 或模板生成的）按时长目标
拆成 N 个 scene，每个 scene 自带：
- narration（来自脚本的某段文字）
- visual_desc（场景视觉描述，给 image prompt 用）
- image_prompt（4K photo 风格的英文 prompt）
- keywords（备用 Pexels 搜索词）
- target_duration（秒）

与改造 A 的关系：
- A 是 "长文本 → 多集" 的宏观拆分。
- C 是 "已写好的脚本 → 分镜" 的微观拆分，给 AI 出图用。

设计：
- 复用 `llm._generate_response` 通道，零侵入。
- 注入式 `llm_caller`，便于测试 mock 和未来扩展。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from loguru import logger

# ---------------------------------------------------------------
# 常量
# ---------------------------------------------------------------
MIN_SCRIPT_LENGTH = 30
MAX_SCENES = 20
MIN_SCENES = 1
DEFAULT_SCENE_DURATION = 6.0  # 秒
MIN_SCENE_DURATION = 2.0
MAX_SCENE_DURATION = 15.0
DEFAULT_MAX_RETRIES = 3

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------
# 错误体系
# ---------------------------------------------------------------
class StoryboardError(Exception):
    """storyboard 统一错误基类。"""


class SceneValidationError(StoryboardError):
    pass


class LLMParseError(StoryboardError):
    pass


# ---------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------
@dataclass(frozen=True)
class Scene:
    scene_id: int
    narration: str
    visual_desc: str
    image_prompt: str
    keywords: Tuple[str, ...]
    target_duration: float

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "narration": self.narration,
            "visual_desc": self.visual_desc,
            "image_prompt": self.image_prompt,
            "keywords": list(self.keywords),
            "target_duration": self.target_duration,
        }


# ---------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------
STORYBOARD_SYSTEM_PROMPT = """
# Role: Short-Video Storyboard Splitter

## Goal
Given a written video script and a target total duration, produce a JSON
array of {min}-{max} scenes for an AI image-based short video.
Each scene maps a slice of the narration to one still image prompt.

## Output Format
Return a single JSON array, nothing else.
- No markdown fences (no ```json), no prose.
- Each scene object MUST have:
  - `scene_id`: int, starting from 1
  - `narration`: str, the spoken slice of the script (verbatim from input)
  - `visual_desc`: str, what's visually happening in the scene (Chinese OK)
  - `image_prompt`: str, EN prompt for a 4K photorealistic still
                   (subject + action + setting + lighting/style)
  - `keywords`: list of 2-5 EN search terms (fallback if image gen fails)
  - `target_duration`: float, seconds in [2, 15]

## Rules
1. Sum of target_duration should be close to the target total duration.
2. narrations must cover the script in order, no omissions, no rewording.
3. image_prompt must be specific and concrete; avoid abstract words.
4. Do NOT invent narration not in the original script.
""".strip()


def render_storyboard_prompt(
    script: str,
    target_total_duration: float,
    n_scenes_hint: Optional[int] = None,
    *,
    min_scenes: int = MIN_SCENES,
    max_scenes: int = MAX_SCENES,
) -> str:
    n_hint = (
        f"Suggested scene count: ~{n_scenes_hint}." if n_scenes_hint else ""
    )
    return (
        f"# Script\n\"\"\"\n{script}\n\"\"\"\n\n"
        f"# Target total duration: {target_total_duration:.1f} seconds\n"
        f"# Scene count range: [{min_scenes}, {max_scenes}]\n"
        f"{n_hint}\n"
        "# Task\n"
        "Split the script following the system rules. Respond with the JSON array only."
    )


# ---------------------------------------------------------------
# 解析
# ---------------------------------------------------------------
def _strip_fence(raw: str) -> str:
    return _FENCE_RE.sub("", raw).strip()


def _coerce_keywords(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(s.strip() for s in value.split(",") if s.strip())
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for s in value:
            if s is None:
                continue
            text = str(s).strip()
            if text:
                out.append(text)
        return tuple(out)
    raise SceneValidationError(
        f"keywords must be list or string, got {type(value).__name__}"
    )


def _coerce_scene(raw: dict, expected_id: int) -> Scene:
    try:
        scene_id = int(raw.get("scene_id", expected_id))
        narration = str(raw["narration"]).strip()
        visual_desc = str(raw["visual_desc"]).strip()
        image_prompt = str(raw["image_prompt"]).strip()
        keywords = _coerce_keywords(raw.get("keywords", []))
        target_duration = float(
            raw.get("target_duration", DEFAULT_SCENE_DURATION)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SceneValidationError(
            f"scene {expected_id} field missing or wrong type: {exc}"
        ) from exc

    if not narration:
        raise SceneValidationError(f"scene {expected_id} narration is empty")
    if not image_prompt:
        raise SceneValidationError(
            f"scene {expected_id} image_prompt is empty"
        )

    target_duration = max(
        MIN_SCENE_DURATION,
        min(MAX_SCENE_DURATION, target_duration),
    )
    return Scene(
        scene_id=scene_id,
        narration=narration,
        visual_desc=visual_desc,
        image_prompt=image_prompt,
        keywords=keywords,
        target_duration=target_duration,
    )


def parse_scenes(raw_response: str) -> List[Scene]:
    cleaned = _strip_fence(raw_response)
    if not cleaned:
        raise LLMParseError("empty LLM response")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        first = cleaned.find("[")
        last = cleaned.rfind("]")
        if first != -1 and last != -1 and last > first:
            try:
                data = json.loads(cleaned[first : last + 1])
            except json.JSONDecodeError as inner:
                raise LLMParseError(f"JSON parse failed: {inner}") from inner
        else:
            raise LLMParseError(f"JSON parse failed: {exc}") from exc
    if not isinstance(data, list):
        raise LLMParseError(f"expected array, got {type(data).__name__}")
    return [_coerce_scene(item, idx + 1) for idx, item in enumerate(data)]


# ---------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------
LLMCaller = Callable[[str, str], str]


def generate_storyboard(
    script: str,
    llm_caller: LLMCaller,
    *,
    target_total_duration: float = 60.0,
    n_scenes_hint: Optional[int] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> List[Scene]:
    """把脚本拆成分镜列表。"""
    script = (script or "").strip()
    if len(script) < MIN_SCRIPT_LENGTH:
        raise StoryboardError(
            f"script too short: {len(script)} < {MIN_SCRIPT_LENGTH}"
        )
    if target_total_duration <= 0:
        raise ValueError("target_total_duration must be > 0")

    user_prompt = render_storyboard_prompt(
        script=script,
        target_total_duration=target_total_duration,
        n_scenes_hint=n_scenes_hint,
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            raw = llm_caller(STORYBOARD_SYSTEM_PROMPT, user_prompt)
            scenes = parse_scenes(raw)
            if not scenes:
                raise SceneValidationError("LLM returned 0 scenes")
            logger.success(
                "storyboard: produced {} scenes on attempt {}",
                len(scenes),
                attempt,
            )
            return scenes
        except (LLMParseError, SceneValidationError) as exc:
            last_error = exc
            logger.warning(
                "storyboard attempt {} failed ({}: {}), retrying",
                attempt,
                type(exc).__name__,
                exc,
            )
    raise StoryboardError(
        f"storyboard failed after {max_retries} attempts; last error: {last_error}"
    )


__all__ = [
    "LLMParseError",
    "Scene",
    "SceneValidationError",
    "StoryboardError",
    "generate_storyboard",
    "parse_scenes",
    "render_storyboard_prompt",
]
