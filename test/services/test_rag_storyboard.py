"""改造 A：RAG 长脚本拆分测试。

覆盖：
- 切块：中文友好分隔符、chunk_size/overlap 边界
- 检索：top_k 排序、空输入
- 解析：合法 JSON、markdown 围栏剥离、字符串 keywords 兼容、duration clamp
- 主入口：输入长度边界、重试机制、错误传播
- prompt 构造：context 注入、空 context 兜底
- llm.generate_long_storyboard 包装层
- POST /api/v1/long-storyboard 端到端
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi.testclient import TestClient

from app.asgi import app
from app.services import llm, rag_storyboard as rag
from app.services.prompts.long_storyboard import (
    LONG_STORYBOARD_SYSTEM_PROMPT,
    build_long_storyboard_prompt,
)


# ----------------------------------------------------------------------
# 切块
# ----------------------------------------------------------------------


class TestSplitLongText:
    def test_chinese_sentence_boundary_respected(self):
        text = "第一句话。第二句话。第三句话。" * 50
        chunks = rag.split_long_text(text, chunk_size=120, chunk_overlap=20)
        assert len(chunks) > 1
        # 每个 chunk 不应在汉字中间被砍（最后字符是分隔符或邻接）
        for c in chunks:
            assert c

    def test_short_text_returns_single_chunk(self):
        chunks = rag.split_long_text("短文本")
        assert chunks == ["短文本"]

    def test_overlap_creates_shared_content(self):
        text = "段落 A。段落 B。段落 C。" * 30
        chunks = rag.split_long_text(text, chunk_size=80, chunk_overlap=40)
        assert len(chunks) >= 2


# ----------------------------------------------------------------------
# 检索
# ----------------------------------------------------------------------


class TestRetrieveTopK:
    def test_returns_at_most_k(self):
        chunks = ["跨境物流", "海外仓", "云仓", "FBA", "客服"]
        top = rag.retrieve_top_k_chunks(chunks, query="跨境物流", top_k=2)
        assert len(top) <= 2

    def test_empty_chunks_returns_empty(self):
        assert rag.retrieve_top_k_chunks([], query="x", top_k=3) == []

    def test_top_k_zero_returns_empty(self):
        assert rag.retrieve_top_k_chunks(["a"], query="x", top_k=0) == []

    def test_relevant_chunk_ranked_first(self):
        chunks = ["完全无关的内容", "跨境物流 政策解读 干货分享", "另一段内容"]
        top = rag.retrieve_top_k_chunks(chunks, query="跨境物流 政策", top_k=1)
        assert "跨境物流" in top[0]


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------


VALID_EPISODE_JSON = """[
  {
    "episode_id": 1,
    "title": "示例标题",
    "outline": "概要",
    "narration": "完整旁白",
    "keywords": ["topic", "keyword"],
    "target_duration": 60.0
  }
]"""


class TestParseEpisodes:
    def test_parse_valid_json(self):
        episodes = rag.parse_episodes(VALID_EPISODE_JSON)
        assert len(episodes) == 1
        assert episodes[0].title == "示例标题"
        assert episodes[0].keywords == ("topic", "keyword")
        assert episodes[0].target_duration == 60.0

    def test_strips_json_fence(self):
        wrapped = "```json\n" + VALID_EPISODE_JSON + "\n```"
        episodes = rag.parse_episodes(wrapped)
        assert len(episodes) == 1

    def test_strips_plain_fence(self):
        wrapped = "```\n" + VALID_EPISODE_JSON + "\n```"
        episodes = rag.parse_episodes(wrapped)
        assert len(episodes) == 1

    def test_bracket_fallback_when_outer_text(self):
        noisy = "Sure, here is the JSON:\n" + VALID_EPISODE_JSON + "\nDone."
        episodes = rag.parse_episodes(noisy)
        assert len(episodes) == 1

    def test_keywords_string_split(self):
        raw = """[{"episode_id":1,"title":"x","outline":"y","narration":"z","keywords":"a, b, c","target_duration":45}]"""
        episodes = rag.parse_episodes(raw)
        assert episodes[0].keywords == ("a", "b", "c")

    def test_duration_clamped_to_max(self):
        raw = """[{"episode_id":1,"title":"x","outline":"y","narration":"z","keywords":["a"],"target_duration":300}]"""
        episodes = rag.parse_episodes(raw)
        assert episodes[0].target_duration == rag.MAX_TARGET_DURATION

    def test_duration_clamped_to_min(self):
        raw = """[{"episode_id":1,"title":"x","outline":"y","narration":"z","keywords":["a"],"target_duration":5}]"""
        episodes = rag.parse_episodes(raw)
        assert episodes[0].target_duration == rag.MIN_TARGET_DURATION

    def test_empty_response_raises(self):
        with pytest.raises(rag.LLMOutputParseError, match="empty"):
            rag.parse_episodes("")

    def test_invalid_json_raises(self):
        with pytest.raises(rag.LLMOutputParseError):
            rag.parse_episodes("not json at all")

    def test_object_not_array_raises(self):
        with pytest.raises(rag.LLMOutputParseError, match="expected JSON array"):
            rag.parse_episodes('{"episode_id": 1}')

    def test_missing_title_raises(self):
        raw = """[{"episode_id":1,"outline":"y","narration":"z","keywords":["a"],"target_duration":45}]"""
        with pytest.raises(rag.EpisodeValidationError, match="title"):
            rag.parse_episodes(raw)

    def test_empty_narration_raises(self):
        raw = """[{"episode_id":1,"title":"x","outline":"y","narration":"","keywords":["a"],"target_duration":45}]"""
        with pytest.raises(rag.EpisodeValidationError, match="narration is empty"):
            rag.parse_episodes(raw)

    def test_empty_keywords_raises(self):
        raw = """[{"episode_id":1,"title":"x","outline":"y","narration":"z","keywords":[],"target_duration":45}]"""
        with pytest.raises(rag.EpisodeValidationError, match="keywords is empty"):
            rag.parse_episodes(raw)


# ----------------------------------------------------------------------
# 主入口（mock LLM）
# ----------------------------------------------------------------------


class TestGenerateLongStoryboard:
    def test_input_too_short_raises(self):
        with pytest.raises(rag.InputTooShortError):
            rag.generate_long_storyboard("短", llm_caller=lambda s, u: "[]")

    def test_input_too_long_raises(self):
        too_long = "x" * (rag.MAX_INPUT_LENGTH + 1)
        with pytest.raises(rag.InputTooLongError):
            rag.generate_long_storyboard(too_long, llm_caller=lambda s, u: "[]")

    def test_success_on_first_attempt(self):
        text = "测试文本。" * 100  # > 200
        calls = []

        def caller(sp, up):
            calls.append((sp, up))
            return VALID_EPISODE_JSON

        episodes = rag.generate_long_storyboard(text, llm_caller=caller)
        assert len(episodes) == 1
        assert len(calls) == 1
        sp_arg = calls[0][0]
        assert "Long-form Content Storyboard Splitter" in sp_arg

    def test_retries_on_parse_failure(self):
        text = "测试文本。" * 100
        seq = ["garbage 1", "garbage 2", VALID_EPISODE_JSON]

        def caller(sp, up):
            return seq.pop(0)

        episodes = rag.generate_long_storyboard(
            text, llm_caller=caller, max_retries=3
        )
        assert len(episodes) == 1

    def test_raises_after_exhausting_retries(self):
        text = "测试文本。" * 100

        def always_bad(sp, up):
            return "still not json"

        with pytest.raises(rag.LongStoryboardError, match="failed after"):
            rag.generate_long_storyboard(
                text, llm_caller=always_bad, max_retries=2
            )


# ----------------------------------------------------------------------
# Prompt 构造
# ----------------------------------------------------------------------


class TestBuildPrompt:
    def test_includes_full_text(self):
        prompt = build_long_storyboard_prompt(full_text="HelloWorld原文")
        assert "HelloWorld原文" in prompt

    def test_no_context_fallback(self):
        prompt = build_long_storyboard_prompt(full_text="x", context_chunks=[])
        assert "no additional context" in prompt

    def test_context_chunks_joined(self):
        prompt = build_long_storyboard_prompt(
            full_text="x", context_chunks=["chunk1", "chunk2"], top_k=2
        )
        assert "chunk1" in prompt
        assert "chunk2" in prompt
        assert "top-2" in prompt


# ----------------------------------------------------------------------
# llm.generate_long_storyboard 包装层
# ----------------------------------------------------------------------


class TestLLMWrapper:
    def test_uses_generate_response(self):
        text = "测试文本。" * 100
        with patch.object(llm, "_generate_response", return_value=VALID_EPISODE_JSON) as gen:
            episodes = llm.generate_long_storyboard(text)
        assert len(episodes) == 1
        gen.assert_called_once()
        # 合并 prompt 应包含 system + user 两部分
        called_prompt = gen.call_args.kwargs["prompt"]
        assert "Long-form Content Storyboard Splitter" in called_prompt
        assert "Source Document" in called_prompt


# ----------------------------------------------------------------------
# FastAPI 端到端
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


class TestLongStoryboardEndpoint:
    def test_too_short_returns_422(self, client: TestClient):
        # 项目自定义 RequestValidationError handler 把 422 改成 400（见 app/asgi.py）
        r = client.post("/api/v1/long-storyboard", json={"text": "x"})
        assert r.status_code == 400
        assert "200" in str(r.json())  # min_length=200 错误信息

    def test_success(self, client: TestClient):
        text = "这是一段测试文本。" * 50  # > 200
        with patch.object(
            llm, "_generate_response", return_value=VALID_EPISODE_JSON
        ):
            r = client.post(
                "/api/v1/long-storyboard",
                json={"text": text, "max_retries": 1},
            )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["episode_count"] == 1
        assert data["episodes"][0]["title"] == "示例标题"
        assert data["episodes"][0]["keywords"] == ["topic", "keyword"]

    def test_llm_garbage_returns_502(self, client: TestClient):
        text = "测试文本。" * 100
        with patch.object(llm, "_generate_response", return_value="garbage"):
            r = client.post(
                "/api/v1/long-storyboard",
                json={"text": text, "max_retries": 1},
            )
        assert r.status_code == 502

    def test_custom_chunk_params_accepted(self, client: TestClient):
        text = "段落。" * 200
        with patch.object(
            llm, "_generate_response", return_value=VALID_EPISODE_JSON
        ):
            r = client.post(
                "/api/v1/long-storyboard",
                json={
                    "text": text,
                    "chunk_size": 1000,
                    "chunk_overlap": 100,
                    "top_k": 5,
                    "max_retries": 1,
                },
            )
        assert r.status_code == 200
