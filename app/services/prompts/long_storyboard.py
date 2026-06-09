"""RAG 长脚本拆分的 prompt 资产（改造 A）。

设计目标：
- 给定长文本（小说/白皮书/长文），让 LLM 把它拆成多集短视频脚本。
- 每集自带 outline / narration / keywords / target_duration。
- 输出**严格 JSON 数组**，避免 markdown 围栏导致解析失败。

约束：
- narration 用与原文一致的语言（默认中文）。
- keywords 强制英文，与 Pexels/Pixabay 搜索通道对齐。
- target_duration 范围 30-90 秒（与 MPT 单集时长一致）。
"""

from __future__ import annotations

# ---------------------------------------------------------------
# System prompt：拆分规则
# ---------------------------------------------------------------
LONG_STORYBOARD_SYSTEM_PROMPT = """
# Role: Long-form Content Storyboard Splitter

## Goal
Given a long-form text (article / whitepaper / book chapter), split it into
N self-contained short-video episodes. Each episode targets 30-90 seconds of
voice-over.

## Output Format
You MUST return a single JSON array, nothing else.
- No markdown fences (no ```json), no prose, no explanation.
- Each element is an Episode object with EXACTLY these fields:
  - `episode_id`: int, starting from 1
  - `title`: str, 6-20 characters in Chinese (or 10-40 in English)
  - `outline`: str, 1-2 sentences summarizing the episode
  - `narration`: str, the spoken voice-over script (NOT the outline);
                 same language as the original text
  - `keywords`: list of 3-7 English search terms for stock-video lookup;
                ALWAYS English even if narration is Chinese
  - `target_duration`: float, target voice-over seconds in [30, 90]

## Splitting Rules
1. Cut at natural content boundaries (sections, scenes, sub-arguments),
   NEVER mid-sentence.
2. Each episode must stand alone — DO NOT rely on phrases like
   "上集说到" or "as we discussed earlier"; reintroduce context briefly.
3. Choose N adaptively: prefer 3-8 episodes per ~5000 characters; longer
   inputs scale up but never produce trivial 1-sentence episodes.
4. narration must be ready-to-speak: no headings, no bullet points,
   no markdown, no parenthetical stage directions.

## Quality Bar
- If the input is too short to split (< 600 chars / equivalent), return a
  single-episode array.
- If the input is corrupted or empty, return `[]`.
- Never invent facts not present in the input.

## Example Output
[
  {
    "episode_id": 1,
    "title": "为什么 800 美元免税门槛动了",
    "outline": "拆解美国小额包裹免税新政的来龙去脉。",
    "narration": "你最近发美区的小包是不是被卡得越来越多……（约 60 秒可朗读文本）",
    "keywords": ["us customs", "small parcel", "tariff change", "ecommerce"],
    "target_duration": 60.0
  }
]
""".strip()


# ---------------------------------------------------------------
# User prompt 模板：把检索上下文 + 原文 + 任务塞进去
# ---------------------------------------------------------------
LONG_STORYBOARD_USER_PROMPT_TEMPLATE = """\
# Source Document
\"\"\"
{full_text}
\"\"\"

# Retrieved Context (most relevant chunks, top-{top_k})
\"\"\"
{context_block}
\"\"\"

# Task
Split the source document into self-contained short-video episodes,
following the system rules. Respond with the JSON array only.
"""


def build_long_storyboard_prompt(
    full_text: str,
    context_chunks: list[str] | None = None,
    top_k: int = 3,
) -> str:
    """拼接最终 user prompt。

    `context_chunks` 是检索得到的 Top-K 上下文段落；为空时只塞原文。
    `top_k` 用来在 prompt 里显式告诉模型"我给了你 K 段最相关上下文"。
    """
    chunks = context_chunks or []
    if chunks:
        context_block = "\n\n---\n\n".join(chunks)
    else:
        context_block = "(no additional context retrieved)"

    return LONG_STORYBOARD_USER_PROMPT_TEMPLATE.format(
        full_text=full_text.strip(),
        context_block=context_block.strip(),
        top_k=top_k,
    )


__all__ = [
    "LONG_STORYBOARD_SYSTEM_PROMPT",
    "LONG_STORYBOARD_USER_PROMPT_TEMPLATE",
    "build_long_storyboard_prompt",
]
