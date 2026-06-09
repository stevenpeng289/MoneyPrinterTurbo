"""RAG 长脚本拆分服务（改造 A）。

输入：长文本（小说 / 白皮书 / 长文）。
输出：多集结构化短视频脚本（每集自带 outline / narration / keywords / 时长）。

设计要点：
- 文本切块用 `langchain-text-splitters.RecursiveCharacterTextSplitter`（中文友好分隔符）。
- 上下文检索 v1 用**滑窗 + 关键词 overlap**（不引入 chromadb，保持轻量）；
  v2 可扩展为本地向量库，留接口位置。
- LLM 调用复用 `app.services.llm._generate_response()` 通道，支持所有 provider。
- 输出严格 JSON 校验：用 Pydantic + 自动剥离 ```json 围栏 + 重试。
- 全模块异常都收敛到 `LongStoryboardError`，便于 controller 层统一返回 4xx。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from loguru import logger

from app.services.prompts.long_storyboard import (
    LONG_STORYBOARD_SYSTEM_PROMPT,
    build_long_storyboard_prompt,
)

# ---------------------------------------------------------------
# 常量
# ---------------------------------------------------------------
MIN_INPUT_LENGTH = 200
MAX_INPUT_LENGTH = 200_000  # 20 万字硬上限，防止 prompt 撑爆 token
DEFAULT_CHUNK_SIZE = 2000
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_TOP_K = 3
DEFAULT_MAX_RETRIES = 3
MIN_TARGET_DURATION = 30.0
MAX_TARGET_DURATION = 90.0

# 中文友好的切分符（langchain 默认只有英文）。
_CHINESE_SEPARATORS = [
    "\n\n",
    "\n",
    "。",
    "！",
    "？",
    ".",
    "!",
    "?",
    "；",
    ";",
    "，",
    ",",
    " ",
    "",
]

# 去 markdown 围栏（LLM 偶尔会包 ```json ... ```）。
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------
# 错误体系
# ---------------------------------------------------------------
class LongStoryboardError(Exception):
    """RAG 长脚本拆分的统一错误基类。"""


class InputTooShortError(LongStoryboardError):
    """输入文本短于 `MIN_INPUT_LENGTH`。"""


class InputTooLongError(LongStoryboardError):
    """输入文本超过 `MAX_INPUT_LENGTH`。"""


class EpisodeValidationError(LongStoryboardError):
    """LLM 返回的 JSON 不能通过 Episode 校验。"""


class LLMOutputParseError(LongStoryboardError):
    """LLM 返回的内容无法解析为 JSON 数组。"""


# ---------------------------------------------------------------
# 数据结构（frozen dataclass，调用方禁止变更）
# ---------------------------------------------------------------
@dataclass(frozen=True)
class EpisodeDraft:
    episode_id: int
    title: str
    outline: str
    narration: str
    keywords: tuple[str, ...]
    target_duration: float

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "title": self.title,
            "outline": self.outline,
            "narration": self.narration,
            "keywords": list(self.keywords),
            "target_duration": self.target_duration,
        }


# ---------------------------------------------------------------
# 切块
# ---------------------------------------------------------------
def split_long_text(
    text: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[str]:
    """用 RecursiveCharacterTextSplitter 切块，中文友好分隔符。

    切空时返回 `[text]`（不抛错，调用方自己处理短文本）。
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=_CHINESE_SEPARATORS,
        length_function=len,
        is_separator_regex=False,
    )
    chunks = splitter.split_text(text)
    return chunks if chunks else [text]


# ---------------------------------------------------------------
# 上下文检索（v1：滑窗 + 关键词 overlap）
# ---------------------------------------------------------------
def _keyword_overlap_score(query: str, candidate: str) -> int:
    """粗略 keyword overlap：两段共享字符 n-gram 数量。"""
    q_set = {query[i : i + 4] for i in range(0, max(len(query) - 4, 0), 4)}
    c_set = {candidate[i : i + 4] for i in range(0, max(len(candidate) - 4, 0), 4)}
    return len(q_set & c_set)


def retrieve_top_k_chunks(
    chunks: Sequence[str],
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> List[str]:
    """从所有 chunk 里选与 query 最相关的 top_k 段。

    v1 实现：n-gram overlap 排序。v2 可换为 embedding 余弦相似度。
    """
    if not chunks or top_k <= 0:
        return []
    scored = [(idx, _keyword_overlap_score(query, c)) for idx, c in enumerate(chunks)]
    # 排序：score desc, idx asc（保留原文档顺序作 tie-breaker）
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [chunks[idx] for idx, _ in scored[:top_k]]


# ---------------------------------------------------------------
# 输出解析与校验
# ---------------------------------------------------------------
def _strip_markdown_fence(raw: str) -> str:
    return _FENCE_RE.sub("", raw).strip()


def _coerce_keywords(value) -> tuple[str, ...]:
    if isinstance(value, str):
        # 兼容模型偶尔返回 "a, b, c" 字符串
        return tuple(s.strip() for s in value.split(",") if s.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(s).strip() for s in value if str(s).strip())
    raise EpisodeValidationError(
        f"keywords must be list or string, got {type(value).__name__}"
    )


def _coerce_episode(raw: dict, expected_id: int) -> EpisodeDraft:
    try:
        episode_id = int(raw.get("episode_id", expected_id))
        title = str(raw["title"]).strip()
        outline = str(raw["outline"]).strip()
        narration = str(raw["narration"]).strip()
        keywords = _coerce_keywords(raw.get("keywords", []))
        target_duration = float(raw.get("target_duration", 60.0))
    except (KeyError, TypeError, ValueError) as exc:
        raise EpisodeValidationError(
            f"episode {expected_id} field missing or wrong type: {exc}"
        ) from exc

    if not title:
        raise EpisodeValidationError(f"episode {expected_id} title is empty")
    if not narration:
        raise EpisodeValidationError(f"episode {expected_id} narration is empty")
    if not keywords:
        raise EpisodeValidationError(f"episode {expected_id} keywords is empty")
    # target_duration clamp 到合法范围，不抛错（LLM 经常超界）
    target_duration = max(MIN_TARGET_DURATION, min(MAX_TARGET_DURATION, target_duration))

    return EpisodeDraft(
        episode_id=episode_id,
        title=title,
        outline=outline,
        narration=narration,
        keywords=keywords,
        target_duration=target_duration,
    )


def parse_episodes(raw_response: str) -> List[EpisodeDraft]:
    """把 LLM 原始响应解析成 EpisodeDraft 列表。

    步骤：剥围栏 → json.loads → 逐项校验。失败抛 `LongStoryboardError` 子类。
    """
    cleaned = _strip_markdown_fence(raw_response)
    if not cleaned:
        raise LLMOutputParseError("LLM returned empty response")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # 尝试抢救：找第一个 [ 和最后一个 ]
        first = cleaned.find("[")
        last = cleaned.rfind("]")
        if first != -1 and last != -1 and last > first:
            try:
                data = json.loads(cleaned[first : last + 1])
            except json.JSONDecodeError as inner:
                raise LLMOutputParseError(
                    f"failed to parse JSON even after bracket fallback: {inner}"
                ) from inner
        else:
            raise LLMOutputParseError(
                f"failed to parse JSON: {exc} | first 200 chars: {cleaned[:200]}"
            ) from exc

    if not isinstance(data, list):
        raise LLMOutputParseError(
            f"expected JSON array, got {type(data).__name__}"
        )

    return [_coerce_episode(item, idx + 1) for idx, item in enumerate(data)]


# ---------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------
LLMCaller = Callable[[str, str], str]


def generate_long_storyboard(
    text: str,
    llm_caller: LLMCaller,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    top_k: int = DEFAULT_TOP_K,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> List[EpisodeDraft]:
    """RAG 长脚本拆分主入口。

    参数：
        text:        原文。
        llm_caller:  形如 `(system_prompt, user_prompt) -> str` 的可调用对象。
                     由 controller 层注入（默认 `app.services.llm._generate_response()` 包装）。
        chunk_size:  切块大小。
        chunk_overlap: 切块重叠。
        top_k:       检索 top-K 上下文。
        max_retries: LLM 输出解析失败时的重试次数。

    返回：`List[EpisodeDraft]`。失败抛 `LongStoryboardError` 子类。

    设计要点：
        - 通过 `llm_caller` 注入 LLM，避免循环导入 + 便于测试 mock。
        - 检索阶段把"原文前 N 字"作为 query，让上下文回到开头方向。
    """
    text = (text or "").strip()
    if len(text) < MIN_INPUT_LENGTH:
        raise InputTooShortError(
            f"input length {len(text)} < {MIN_INPUT_LENGTH}"
        )
    if len(text) > MAX_INPUT_LENGTH:
        raise InputTooLongError(
            f"input length {len(text)} > {MAX_INPUT_LENGTH}"
        )

    chunks = split_long_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    logger.info(
        "rag_storyboard: split into {} chunks (chunk_size={}, overlap={})",
        len(chunks),
        chunk_size,
        chunk_overlap,
    )

    # 用首段作为检索 query，让 top-K 偏向"核心论点"段落。
    query = text[: min(len(text), chunk_size)]
    context_chunks = retrieve_top_k_chunks(chunks, query=query, top_k=top_k)

    user_prompt = build_long_storyboard_prompt(
        full_text=text,
        context_chunks=context_chunks,
        top_k=top_k,
    )

    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            raw = llm_caller(LONG_STORYBOARD_SYSTEM_PROMPT, user_prompt)
            episodes = parse_episodes(raw)
            logger.success(
                "rag_storyboard: produced {} episodes on attempt {}",
                len(episodes),
                attempt,
            )
            return episodes
        except (LLMOutputParseError, EpisodeValidationError) as exc:
            last_error = exc
            logger.warning(
                "rag_storyboard: attempt {} failed ({}: {}), retrying...",
                attempt,
                type(exc).__name__,
                exc,
            )

    raise LongStoryboardError(
        f"failed after {max_retries} attempts; last error: {last_error}"
    )


__all__ = [
    "EpisodeDraft",
    "EpisodeValidationError",
    "InputTooLongError",
    "InputTooShortError",
    "LLMOutputParseError",
    "LongStoryboardError",
    "generate_long_storyboard",
    "parse_episodes",
    "retrieve_top_k_chunks",
    "split_long_text",
]
