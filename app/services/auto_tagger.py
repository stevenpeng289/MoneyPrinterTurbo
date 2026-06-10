"""本地视频自动打标服务（改造 D-1）。

工作流：
  ffmpeg 抽 N 帧（开头/中间/结尾）→ 多模态 LLM 描述 → 解析 tags →
  写到 `<video>.yaml`（兼容 `material.search_videos_local()` 现有读取格式）。

设计要点：
- **不重写** `search_videos_local()`；只补齐"自动生成 yaml"这一段缺口。
- LLM 调用通过 `tag_caller` 注入，避免循环导入 + 便于测试 mock。
- ffmpeg 用 `ffmpeg-python`（已装），帧文件落到调用方指定的 `frames_dir`。
- **人工保护**：若同目录存在 `<video>.yaml.locked`，跳过覆盖。
- 路径安全：`base_dir` 必填，所有视频/帧路径都通过 `file_security` 校验。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import ffmpeg
import yaml
from loguru import logger

from app.utils.file_security import resolve_path_within_directory


# ---------------------------------------------------------------
# 常量
# ---------------------------------------------------------------
DEFAULT_NUM_FRAMES = 3
MAX_NUM_FRAMES = 6
MIN_VIDEO_DURATION = 1.0  # 秒
LOCKED_SUFFIX = ".locked"
DEFAULT_TAGS_MAX = 10
DEFAULT_TAGS_MIN = 3

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------
# 错误体系
# ---------------------------------------------------------------
class AutoTagError(Exception):
    """auto_tagger 统一错误基类。"""


class VideoProbeError(AutoTagError):
    """ffmpeg 无法解析视频。"""


class FrameExtractionError(AutoTagError):
    """ffmpeg 抽帧失败。"""


class TagParseError(AutoTagError):
    """LLM 返回的 tags 无法解析。"""


# ---------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------
@dataclass(frozen=True)
class AutoTagResult:
    video_path: str
    yaml_path: str
    tags: Tuple[str, ...]
    frames_extracted: int
    skipped_locked: bool


# ---------------------------------------------------------------
# ffmpeg 抽帧
# ---------------------------------------------------------------
def _probe_duration(video_path: str) -> float:
    """ffprobe 取视频时长（秒）。"""
    try:
        meta = ffmpeg.probe(video_path)
    except ffmpeg.Error as exc:
        raise VideoProbeError(f"ffprobe failed for {video_path}: {exc.stderr.decode(errors='ignore') if exc.stderr else exc}") from exc

    duration = 0.0
    fmt = meta.get("format") or {}
    if "duration" in fmt:
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = 0.0
    if duration <= 0.0:
        for stream in meta.get("streams", []):
            if stream.get("codec_type") == "video" and "duration" in stream:
                try:
                    duration = float(stream["duration"])
                    break
                except (TypeError, ValueError):
                    continue
    if duration < MIN_VIDEO_DURATION:
        raise VideoProbeError(
            f"video duration {duration:.2f}s too short for {video_path}"
        )
    return duration


def _pick_timestamps(duration: float, num_frames: int) -> List[float]:
    """选 N 个抽帧时间点：开头一点、均匀分布、结尾一点。

    避免取 0 秒（很多视频开头是黑屏）和 duration（防越界）。
    """
    if num_frames <= 0:
        return []
    if num_frames == 1:
        return [duration / 2.0]
    # 在 [5%, 95%] 范围内均匀采样
    start = duration * 0.05
    end = duration * 0.95
    if end <= start:
        return [duration / 2.0]
    step = (end - start) / (num_frames - 1)
    return [start + step * i for i in range(num_frames)]


def extract_frames(
    video_path: str,
    output_dir: str,
    *,
    num_frames: int = DEFAULT_NUM_FRAMES,
) -> List[str]:
    """从视频抽 N 帧到 `output_dir`，返回帧路径列表（按时间顺序）。

    帧文件名：`{video_basename}_frame_{idx}.jpg`。
    """
    if num_frames <= 0 or num_frames > MAX_NUM_FRAMES:
        raise ValueError(
            f"num_frames must be in [1, {MAX_NUM_FRAMES}], got {num_frames}"
        )
    if not os.path.isfile(video_path):
        raise FrameExtractionError(f"video not found: {video_path}")
    os.makedirs(output_dir, exist_ok=True)

    duration = _probe_duration(video_path)
    timestamps = _pick_timestamps(duration, num_frames)
    basename = os.path.splitext(os.path.basename(video_path))[0]

    frame_paths: List[str] = []
    for idx, ts in enumerate(timestamps, start=1):
        out_path = os.path.join(output_dir, f"{basename}_frame_{idx}.jpg")
        try:
            (
                ffmpeg.input(video_path, ss=ts)
                .output(out_path, vframes=1, format="image2", vcodec="mjpeg")
                .overwrite_output()
                .run(quiet=True, capture_stdout=True, capture_stderr=True)
            )
        except ffmpeg.Error as exc:
            stderr = exc.stderr.decode(errors="ignore") if exc.stderr else str(exc)
            raise FrameExtractionError(
                f"ffmpeg failed to extract frame {idx} at {ts:.2f}s: {stderr[:200]}"
            ) from exc
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            raise FrameExtractionError(f"frame {idx} not written: {out_path}")
        frame_paths.append(out_path)
    return frame_paths


# ---------------------------------------------------------------
# LLM tag 提示词（与 multimodal LLM 通用）
# ---------------------------------------------------------------
TAG_SYSTEM_PROMPT = """
# Role: Video Auto-Tagger

## Goal
Given video frame descriptions, produce a JSON array of {min}-{max} English
tags describing the content. Tags align with stock-video search terms
(used downstream by Pexels/Pixabay/local search).

## Output Format
Return a single JSON array of strings, nothing else.
- Lowercase, single words or short 2-3 word phrases.
- English only (even if the source video is non-English).
- Concrete nouns/actions; avoid abstract adjectives ("beautiful", "amazing").
- Do NOT wrap in markdown fences.

## Example
["warehouse", "forklift", "cargo box", "logistics worker"]
""".strip()


def render_tag_user_prompt(
    filename: str,
    frame_paths: List[str],
    *,
    extra_context: str = "",
    min_tags: int = DEFAULT_TAGS_MIN,
    max_tags: int = DEFAULT_TAGS_MAX,
) -> str:
    """构造 user prompt。frame_paths 由具体 LLM provider 转成 image input。"""
    base = (
        f"Filename: {filename}\n"
        f"Frames extracted: {len(frame_paths)}\n"
        f"Frame paths (for caller to attach as image inputs): {frame_paths}\n"
        f"Tag count target: between {min_tags} and {max_tags}.\n"
        # Reasoning model (e.g. minimax-M2.7) 偶发只输出思考块没出 JSON。
        # 末尾加硬约束减少概率：必须以 JSON 数组结尾。
        "IMPORTANT: Output ONLY a single JSON array of strings, "
        "no other text, no explanation, no markdown fences."
    )
    if extra_context:
        base += f"\nAdditional context: {extra_context}"
    return base


# ---------------------------------------------------------------
# tag 解析
# ---------------------------------------------------------------
def _strip_markdown_fence(raw: str) -> str:
    cleaned = _FENCE_RE.sub("", raw).strip()
    # Reasoning model (e.g. minimax-M2.7, deepseek-r1) 输出会带 <think>...</think>
    # 思考块。剥掉，否则 JSON.loads 失败。
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL).strip()
    return cleaned


def parse_tags(raw_response: str) -> Tuple[str, ...]:
    """把 LLM 原始响应解析成 tag tuple。

    剥围栏 → json.loads → 校验每项为非空 str → 去重去空白。
    """
    cleaned = _strip_markdown_fence(raw_response)
    if not cleaned:
        raise TagParseError("empty response")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # 抢救：找第一个 [ 和最后一个 ]
        first = cleaned.find("[")
        last = cleaned.rfind("]")
        if first != -1 and last != -1 and last > first:
            try:
                data = json.loads(cleaned[first : last + 1])
            except json.JSONDecodeError as inner:
                raise TagParseError(f"JSON parse failed: {inner}") from inner
        else:
            raise TagParseError(f"JSON parse failed: {exc}") from exc

    if not isinstance(data, list):
        raise TagParseError(f"expected array, got {type(data).__name__}")

    seen: set[str] = set()
    tags: List[str] = []
    for item in data:
        # JSON null 显式跳过；不能 str(None) 被当成 "none" 当成有效 tag
        if item is None:
            continue
        s = str(item).strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        tags.append(s)
    if not tags:
        raise TagParseError("no valid tags in response")
    return tuple(tags)


# ---------------------------------------------------------------
# yaml 读写（兼容 search_videos_local 的格式）
# ---------------------------------------------------------------
def _is_locked(yaml_path: str) -> bool:
    return os.path.isfile(yaml_path + LOCKED_SUFFIX)


def write_tags_yaml(
    yaml_path: str,
    tags: Tuple[str, ...],
    *,
    source: str = "auto_tagger",
    model: str = "",
    extra: Optional[dict] = None,
) -> None:
    """写入 tags yaml，与 `search_videos_local()` 读取格式一致。

    格式：
        tags: [t1, t2, ...]
        source: auto_tagger
        model: <provider:model_name>
        ... extra fields
    """
    payload = {
        "tags": list(tags),
        "source": source,
    }
    if model:
        payload["model"] = model
    if extra:
        payload.update(extra)
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            payload, f, allow_unicode=True, sort_keys=False, default_flow_style=False
        )


# ---------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------
TagCaller = Callable[[str, str, List[str]], str]
"""tag_caller(system_prompt, user_prompt, frame_paths) -> raw_response

frame_paths 由具体 caller 决定怎么传给多模态 LLM（OpenAI vision 用 image_url，
Gemini 用 inline_data 等）。
"""


def tag_video(
    video_path: str,
    base_dir: str,
    tag_caller: TagCaller,
    *,
    num_frames: int = DEFAULT_NUM_FRAMES,
    overwrite: bool = False,
    frames_dir: Optional[str] = None,
    model_label: str = "",
    extra_context: str = "",
) -> AutoTagResult:
    """给单个视频自动打标。

    参数：
        video_path:     视频路径（绝对/相对都行，必须在 base_dir 内）。
        base_dir:       素材根目录（路径白名单基线，防 path traversal）。
        tag_caller:     LLM 调用函数（接收 system + user prompt + frames，返回原始响应）。
        num_frames:     抽帧数。
        overwrite:      已存在 yaml 时是否覆盖（除非有 .locked 文件）。
        frames_dir:     抽帧落盘目录；None 时用临时目录（函数返回后清理）。
        model_label:    yaml 里记录的 model 字段，便于追溯。
        extra_context:  user prompt 里附加上下文（如行业、风格提示）。

    返回 `AutoTagResult`。若 yaml 被 lock 或 overwrite=False 且 yaml 已存在，
    `skipped_locked=True`、`tags` 保留为现有 yaml 读到的内容（不抛错）。
    """
    safe_video = resolve_path_within_directory(base_dir, video_path)
    yaml_path = os.path.splitext(safe_video)[0] + ".yaml"

    # 锁定保护：哪怕 overwrite=True 也不动 locked
    if _is_locked(yaml_path):
        logger.info(f"auto_tagger: yaml locked, skipping {yaml_path}")
        existing = _read_existing_tags(yaml_path)
        return AutoTagResult(
            video_path=safe_video,
            yaml_path=yaml_path,
            tags=existing,
            frames_extracted=0,
            skipped_locked=True,
        )

    # 已有 yaml 且不允许覆盖：直接复用
    if os.path.isfile(yaml_path) and not overwrite:
        logger.info(f"auto_tagger: yaml exists, reusing {yaml_path}")
        existing = _read_existing_tags(yaml_path)
        return AutoTagResult(
            video_path=safe_video,
            yaml_path=yaml_path,
            tags=existing,
            frames_extracted=0,
            skipped_locked=False,
        )

    # 抽帧（用临时目录或调用方指定）
    cleanup_frames_dir = False
    effective_frames_dir = frames_dir
    if effective_frames_dir is None:
        effective_frames_dir = tempfile.mkdtemp(prefix="auto_tagger_")
        cleanup_frames_dir = True

    try:
        frames = extract_frames(
            safe_video, effective_frames_dir, num_frames=num_frames
        )
        logger.info(
            f"auto_tagger: extracted {len(frames)} frames from {safe_video}"
        )

        user_prompt = render_tag_user_prompt(
            filename=os.path.basename(safe_video),
            frame_paths=frames,
            extra_context=extra_context,
        )
        # Reasoning model (e.g. minimax-M2.7) 偶发只输出思考块没出 JSON。
        # 失败时重试 2 次 (共 3 次尝试)，仍失败才报错——
        # 保证 auto_tagger 不因 LLM 偶发抖动挂掉。
        last_exc: Optional[Exception] = None
        tags: Tuple[str, ...] = ()
        for attempt in range(1, 4):
            try:
                raw = tag_caller(TAG_SYSTEM_PROMPT, user_prompt, frames)
                tags = parse_tags(raw)
                last_exc = None
                break
            except TagParseError as exc:
                last_exc = exc
                logger.warning(
                    f"auto_tagger: parse failed attempt {attempt}/3 ({exc})"
                )
        if last_exc is not None or not tags:
            raise last_exc if last_exc else TagParseError("no tags after retries")

        write_tags_yaml(
            yaml_path, tags, source="auto_tagger", model=model_label
        )
        logger.success(
            f"auto_tagger: wrote {len(tags)} tags to {yaml_path}"
        )
        return AutoTagResult(
            video_path=safe_video,
            yaml_path=yaml_path,
            tags=tags,
            frames_extracted=len(frames),
            skipped_locked=False,
        )
    finally:
        if cleanup_frames_dir and effective_frames_dir:
            # 清理临时帧文件，避免堆积
            for f in os.listdir(effective_frames_dir):
                try:
                    os.remove(os.path.join(effective_frames_dir, f))
                except OSError:
                    pass
            try:
                os.rmdir(effective_frames_dir)
            except OSError:
                pass


def _read_existing_tags(yaml_path: str) -> Tuple[str, ...]:
    """读已有 yaml 的 tags 字段，失败/缺字段返空 tuple。"""
    if not os.path.isfile(yaml_path):
        return tuple()
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
        tags = meta.get("tags") or []
        if not isinstance(tags, list):
            return tuple()
        return tuple(str(t).strip().lower() for t in tags if str(t).strip())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"auto_tagger: failed to read existing yaml {yaml_path}: {exc}")
        return tuple()


__all__ = [
    "AutoTagError",
    "AutoTagResult",
    "FrameExtractionError",
    "TagParseError",
    "VideoProbeError",
    "extract_frames",
    "parse_tags",
    "render_tag_user_prompt",
    "tag_video",
    "write_tags_yaml",
    "TAG_SYSTEM_PROMPT",
]
