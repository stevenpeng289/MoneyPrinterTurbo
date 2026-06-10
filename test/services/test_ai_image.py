"""改造 C 测试：ai_image + storyboard + consistency_filter + material.download_images_ai。

覆盖：
- ai_image provider 注册表 / 缺 key / OpenAI 兼容调用
- storyboard parse / generate / retry / 错误传播
- consistency_filter best_index / 失败降级
- material.download_images_ai 主链路 + 多候选 VLM 选最佳 + 失败降级
- task.get_video_materials 的 ai_image 分支
- VideoParams 新字段
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.config import config
from app.models.schema import VideoParams
from app.services import (
    ai_image as ai,
    consistency_filter as cf,
    material as m,
    storyboard as sb,
    task as task_mod,
)


# ----------------------------------------------------------------------
# ai_image
# ----------------------------------------------------------------------


class TestAIImageProvider:
    def test_unsupported_provider_raises(self):
        with pytest.raises(ai.ImageProviderUnavailableError):
            ai.get_provider("nope_xxx")

    def test_missing_api_key_raises(self):
        with patch.dict(
            config.app, {"openai_api_key": "", "llm_provider": "openai"}
        ):
            with pytest.raises(ai.ImageProviderUnavailableError, match="missing api key"):
                ai.get_provider("openai")

    def test_default_provider_uses_llm_provider(self):
        with patch.dict(
            config.app,
            {"openai_api_key": "fake", "llm_provider": "openai", "image_provider": ""},
        ):
            p = ai.get_provider()
            assert p.name == "openai"

    def test_generate_image_persists_b64(self, tmp_path):
        fake_item = MagicMock()
        fake_item.b64_json = "aGVsbG8="  # 'hello'
        fake_item.url = None
        fake_response = MagicMock()
        fake_response.data = [fake_item]

        with patch.dict(config.app, {"openai_api_key": "fake"}):
            p = ai.get_provider("openai")
            with patch.object(p.client.images, "generate", return_value=fake_response):
                imgs = p.generate("a cat", n=1, output_dir=str(tmp_path))
        assert len(imgs) == 1
        assert imgs[0].local_path.endswith(".png")
        assert Path(imgs[0].local_path).read_bytes() == b"hello"
        assert imgs[0].provider == "openai"

    def test_generate_image_validates_n(self, tmp_path):
        with patch.dict(config.app, {"openai_api_key": "fake"}):
            p = ai.get_provider("openai")
            with pytest.raises(ValueError, match="n must be in"):
                p.generate("x", n=0)
            with pytest.raises(ValueError, match="n must be in"):
                p.generate("x", n=11)

    def test_api_failure_raises_generation_error(self, tmp_path):
        with patch.dict(config.app, {"openai_api_key": "fake"}):
            p = ai.get_provider("openai")
            with patch.object(
                p.client.images, "generate", side_effect=RuntimeError("boom")
            ):
                with pytest.raises(ai.ImageGenerationError, match="boom"):
                    p.generate("x", n=1, output_dir=str(tmp_path))


# ----------------------------------------------------------------------
# minimax 私有 schema provider
# ----------------------------------------------------------------------


class TestMinimaxImageProvider:
    """minimax 走私有 schema: `POST {base}/image_generation`
    区别于 OpenAI 的 `/images/generations`。
    """

    def _patch_config(self, **overrides):
        defaults = {
            "minimax_api_key": "fake_key",
            "minimax_base_url": "https://api.minimaxi.com/v1",
            "minimax_image_model_name": "image-01",
        }
        defaults.update(overrides)
        return patch.dict(config.app, defaults)

    def test_get_provider_routes_minimax(self):
        """`image_provider=minimax` 必须路由到 MinimaxImageProvider, 不是 OpenAI 兼容类."""
        with self._patch_config(image_provider="minimax"):
            p = ai.get_provider("minimax")
        assert isinstance(p, ai.MinimaxImageProvider)
        assert p.name == "minimax"
        assert p.model == "image-01"
        assert p.base_url == "https://api.minimaxi.com/v1"

    def test_missing_api_key_raises(self):
        with self._patch_config(minimax_api_key=""):
            with pytest.raises(ai.ImageProviderUnavailableError, match="minimax"):
                ai.get_provider("minimax")

    def test_size_to_aspect_ratio_mapping(self):
        with self._patch_config():
            p = ai.get_provider("minimax")
        # 1:1
        assert p._size_to_aspect_ratio("1024x1024") == "1:1"
        # portrait
        assert p._size_to_aspect_ratio("1024x1792") == "9:16"
        # landscape
        assert p._size_to_aspect_ratio("1792x1024") == "16:9"
        # 未知格式 → 1:1 fallback
        assert p._size_to_aspect_ratio("garbage") == "1:1"
        # 大小写兼容
        assert p._size_to_aspect_ratio("1024X1024") == "1:1"

    def test_request_body_format(self, tmp_path):
        """私有 schema body 字段必须正确 (model/prompt/aspect_ratio/response_format/n),
        reference_images 必须转 subject_reference."""
        with self._patch_config():
            p = ai.get_provider("minimax")
        with patch.object(ai.requests, "post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {
                "data": {"image_base64": ["aGVsbG8="]},  # 'hello'
                "base_resp": {"status_code": 0, "status_msg": "ok"},
            }
            p.generate(
                "a cat",
                n=2,
                size="1024x1024",
                reference_images=["/tmp/ref1.png", "/tmp/ref2.png"],
                output_dir=str(tmp_path),
            )
        # 抓请求
        assert post.call_count == 1
        url = post.call_args.args[0]
        body = post.call_args.kwargs["json"]
        # 端点是私有 schema, 不是 OpenAI /images/generations
        assert url == "https://api.minimaxi.com/v1/image_generation"
        # body 字段
        assert body["model"] == "image-01"
        assert body["prompt"] == "a cat"
        assert body["aspect_ratio"] == "1:1"
        assert body["response_format"] == "base64"
        assert body["n"] == 2
        # reference_images 转换
        assert body["subject_reference"] == [
            {"type": "character", "image_file": "/tmp/ref1.png"},
            {"type": "character", "image_file": "/tmp/ref2.png"},
        ]
        # auth header
        headers = post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer fake_key"

    def test_http_error_raises(self, tmp_path):
        with self._patch_config():
            p = ai.get_provider("minimax")
        with patch.object(ai.requests, "post") as post:
            post.return_value.status_code = 401
            post.return_value.text = "unauthorized"
            with pytest.raises(ai.ImageGenerationError, match="HTTP 401"):
                p.generate("x", n=1, output_dir=str(tmp_path))

    def test_business_error_raises(self, tmp_path):
        """HTTP 200 但 base_resp.status_code != 0 视为业务失败."""
        with self._patch_config():
            p = ai.get_provider("minimax")
        with patch.object(ai.requests, "post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {
                "data": {"image_base64": ["aGVsbG8="]},
                "base_resp": {"status_code": 1001, "status_msg": "rate limited"},
            }
            with pytest.raises(ai.ImageGenerationError, match="biz error 1001"):
                p.generate("x", n=1, output_dir=str(tmp_path))

    def test_persists_b64_response(self, tmp_path):
        """image_base64 数组正确解码落盘."""
        with self._patch_config():
            p = ai.get_provider("minimax")
        with patch.object(ai.requests, "post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {
                "data": {"image_base64": ["aGVsbG8=", "d29ybGQ="]},  # 'hello' / 'world'
                "base_resp": {"status_code": 0, "status_msg": "ok"},
            }
            imgs = p.generate("x", n=2, output_dir=str(tmp_path))
        assert len(imgs) == 2
        assert Path(imgs[0].local_path).read_bytes() == b"hello"
        assert Path(imgs[1].local_path).read_bytes() == b"world"
        assert imgs[0].provider == "minimax"
        # 文件名带 minimax_ 前缀
        for img in imgs:
            assert Path(img.local_path).name.startswith("minimax_")

    def test_fallback_to_image_urls(self, tmp_path):
        """没有 image_base64 但有 image_urls 时走 URL 下载通道."""
        with self._patch_config():
            p = ai.get_provider("minimax")
        with patch.object(ai.requests, "post") as post, \
             patch.object(ai.requests, "get") as get:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {
                "data": {"image_urls": ["https://cdn.example.com/a.png"]},
                "base_resp": {"status_code": 0, "status_msg": "ok"},
            }
            get.return_value.status_code = 200
            get.return_value.content = b"url-bytes"
            imgs = p.generate("x", n=1, output_dir=str(tmp_path))
        assert len(imgs) == 1
        assert Path(imgs[0].local_path).read_bytes() == b"url-bytes"
        assert imgs[0].url == "https://cdn.example.com/a.png"

    def test_empty_both_raises(self, tmp_path):
        with self._patch_config():
            p = ai.get_provider("minimax")
        with patch.object(ai.requests, "post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {
                "data": {},
                "base_resp": {"status_code": 0, "status_msg": "ok"},
            }
            with pytest.raises(ai.ImageGenerationError, match="empty"):
                p.generate("x", n=1, output_dir=str(tmp_path))

    def test_n_range_validated(self, tmp_path):
        """minimax n 限制 [1, 9], 跟 OpenAI 的 [1, 10] 不同."""
        with self._patch_config():
            p = ai.get_provider("minimax")
        with pytest.raises(ValueError, match="minimax n must be in"):
            p.generate("x", n=0, output_dir=str(tmp_path))
        with pytest.raises(ValueError, match="minimax n must be in"):
            p.generate("x", n=10, output_dir=str(tmp_path))


# ----------------------------------------------------------------------
# storyboard
# ----------------------------------------------------------------------


VALID_SCENE_JSON = """[
  {
    "scene_id": 1,
    "narration": "开篇旁白",
    "visual_desc": "warehouse with forklifts",
    "image_prompt": "4k photorealistic warehouse, forklifts loading boxes",
    "keywords": ["warehouse", "logistics"],
    "target_duration": 6.0
  }
]"""


class TestStoryboardParse:
    def test_parse_valid(self):
        scenes = sb.parse_scenes(VALID_SCENE_JSON)
        assert len(scenes) == 1
        assert scenes[0].image_prompt.startswith("4k photo")
        assert scenes[0].keywords == ("warehouse", "logistics")

    def test_parse_fenced(self):
        scenes = sb.parse_scenes("```json\n" + VALID_SCENE_JSON + "\n```")
        assert len(scenes) == 1

    def test_duration_clamp_max(self):
        raw = '[{"scene_id":1,"narration":"n","visual_desc":"v","image_prompt":"p","keywords":["a"],"target_duration":99}]'
        assert sb.parse_scenes(raw)[0].target_duration == sb.MAX_SCENE_DURATION

    def test_duration_clamp_min(self):
        raw = '[{"scene_id":1,"narration":"n","visual_desc":"v","image_prompt":"p","keywords":["a"],"target_duration":0.5}]'
        assert sb.parse_scenes(raw)[0].target_duration == sb.MIN_SCENE_DURATION

    def test_missing_narration_raises(self):
        raw = '[{"scene_id":1,"narration":"","visual_desc":"v","image_prompt":"p","keywords":["a"],"target_duration":5}]'
        with pytest.raises(sb.SceneValidationError, match="narration is empty"):
            sb.parse_scenes(raw)

    def test_missing_image_prompt_raises(self):
        raw = '[{"scene_id":1,"narration":"n","visual_desc":"v","image_prompt":"","keywords":["a"],"target_duration":5}]'
        with pytest.raises(sb.SceneValidationError, match="image_prompt is empty"):
            sb.parse_scenes(raw)

    def test_invalid_json_raises(self):
        with pytest.raises(sb.LLMParseError):
            sb.parse_scenes("not json")


class TestStoryboardGenerate:
    def test_too_short_raises(self):
        with pytest.raises(sb.StoryboardError, match="too short"):
            sb.generate_storyboard(
                script="短", llm_caller=lambda s, u: VALID_SCENE_JSON
            )

    def test_success(self):
        script = "这是一段足够长的脚本内容。" * 5
        scenes = sb.generate_storyboard(
            script=script,
            target_total_duration=30.0,
            llm_caller=lambda s, u: VALID_SCENE_JSON,
        )
        assert len(scenes) == 1

    def test_retry_on_parse_failure(self):
        script = "这是一段足够长的脚本内容。" * 5
        seq = ["garbage", VALID_SCENE_JSON]
        scenes = sb.generate_storyboard(
            script=script,
            llm_caller=lambda s, u: seq.pop(0),
            max_retries=2,
        )
        assert len(scenes) == 1

    def test_exhausts_retries(self):
        script = "这是一段足够长的脚本内容。" * 5
        with pytest.raises(sb.StoryboardError, match="failed after"):
            sb.generate_storyboard(
                script=script,
                llm_caller=lambda s, u: "still garbage",
                max_retries=2,
            )


# ----------------------------------------------------------------------
# consistency_filter
# ----------------------------------------------------------------------


class TestConsistencyFilter:
    def _make_candidates(self, n: int):
        return [
            ai.GeneratedImage(local_path=f"/tmp/c{i}.png", prompt="p", provider="x")
            for i in range(n)
        ]

    def test_parse_best_index_json(self):
        assert cf.parse_best_index('{"best_index":2}', n_candidates=4) == 2

    def test_parse_best_index_regex_fallback(self):
        assert cf.parse_best_index('Sure: "best_index": 1!', n_candidates=4) == 1

    def test_parse_best_index_out_of_range_returns_zero(self):
        assert cf.parse_best_index('{"best_index":99}', n_candidates=3) == 0

    def test_parse_best_index_garbage_returns_zero(self):
        assert cf.parse_best_index("garbage", n_candidates=3) == 0

    def test_single_candidate_returns_self(self):
        cands = self._make_candidates(1)
        assert cf.select_best_image(cands, "x").local_path == "/tmp/c0.png"

    def test_no_vlm_returns_first(self):
        cands = self._make_candidates(3)
        assert cf.select_best_image(cands, "x", vlm_caller=None).local_path == "/tmp/c0.png"

    def test_vlm_picks_second(self):
        cands = self._make_candidates(4)
        chosen = cf.select_best_image(
            cands, "x", vlm_caller=lambda s, u, f: '{"best_index":2}'
        )
        assert chosen.local_path == "/tmp/c2.png"

    def test_vlm_failure_falls_back_to_first(self):
        cands = self._make_candidates(3)
        chosen = cf.select_best_image(
            cands,
            "x",
            vlm_caller=lambda s, u, f: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert chosen.local_path == "/tmp/c0.png"

    def test_empty_candidates_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            cf.select_best_image([], "x")


# ----------------------------------------------------------------------
# material.download_images_ai
# ----------------------------------------------------------------------


def _make_scene(image_prompt="4k photo", target_duration=5.0):
    return sb.Scene(
        scene_id=1,
        narration="n",
        visual_desc="v",
        image_prompt=image_prompt,
        keywords=("a",),
        target_duration=target_duration,
    )


class TestDownloadImagesAI:
    def test_empty_scenes_returns_empty(self):
        assert m.download_images_ai("task", []) == []

    def test_single_candidate(self):
        scene = _make_scene()
        fake_img = ai.GeneratedImage(local_path="/tmp/x.png", prompt="p", provider="openai")
        with patch.object(ai, "generate_image", return_value=[fake_img]):
            paths = m.download_images_ai("task", [scene])
        assert paths == ["/tmp/x.png"]

    def test_multi_candidate_vlm_select(self):
        scene = _make_scene()
        cands = [
            ai.GeneratedImage(local_path=f"/tmp/c{i}.png", prompt="p", provider="openai")
            for i in range(3)
        ]
        with patch.object(ai, "generate_image", return_value=cands):
            paths = m.download_images_ai(
                "task",
                [scene],
                n_candidates=3,
                select_best_caller=lambda s, u, f: '{"best_index":2}',
            )
        assert paths == ["/tmp/c2.png"]

    def test_skips_empty_prompt(self):
        scene = _make_scene(image_prompt="")
        with patch.object(ai, "generate_image") as gen:
            paths = m.download_images_ai("task", [scene])
        gen.assert_not_called()
        assert paths == []

    def test_skip_on_generation_failure(self):
        scenes = [_make_scene() for _ in range(3)]
        with patch.object(
            ai,
            "generate_image",
            side_effect=ai.ImageGenerationError("api down"),
        ):
            paths = m.download_images_ai("task", scenes)
        assert paths == []

    def test_partial_failure_continues(self):
        scenes = [_make_scene() for _ in range(3)]
        results = [
            [ai.GeneratedImage(local_path="/tmp/a.png", prompt="p", provider="x")],
            ai.ImageGenerationError("scene 2 down"),
            [ai.GeneratedImage(local_path="/tmp/c.png", prompt="p", provider="x")],
        ]

        def fake_gen(prompt, **kwargs):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch.object(ai, "generate_image", side_effect=fake_gen):
            paths = m.download_images_ai("task", scenes)
        assert paths == ["/tmp/a.png", "/tmp/c.png"]

    def test_audio_duration_stops_early(self):
        # 3 个 scene 每个 5s，audio_duration 8s 应在第二个 scene 后停
        scenes = [_make_scene(target_duration=5.0) for _ in range(3)]
        fake_img = ai.GeneratedImage(local_path="/tmp/x.png", prompt="p", provider="x")
        with patch.object(ai, "generate_image", return_value=[fake_img]):
            paths = m.download_images_ai("task", scenes, audio_duration=8.0)
        assert len(paths) == 2


# ----------------------------------------------------------------------
# task.get_video_materials ai_image 分支
# ----------------------------------------------------------------------


class TestTaskAIImageBranch:
    def test_ai_image_requires_script(self):
        params = VideoParams(
            video_subject="x", video_source="ai_image", video_script=""
        )
        with patch.object(task_mod.sm.state, "update_task"):
            result = task_mod.get_video_materials(
                "task-id", params, video_terms=["x"], audio_duration=30.0
            )
        assert result is None

    def test_ai_image_storyboard_failure_returns_none(self):
        params = VideoParams(
            video_subject="x",
            video_source="ai_image",
            video_script="一段足够长的脚本内容。" * 10,
        )
        with patch.object(sb, "generate_storyboard", side_effect=sb.StoryboardError("boom")), \
             patch.object(task_mod.sm.state, "update_task"):
            result = task_mod.get_video_materials(
                "task-id", params, video_terms=["x"], audio_duration=30.0
            )
        assert result is None

    def test_ai_image_zero_images_falls_back_to_pexels(self):
        params = VideoParams(
            video_subject="x",
            video_source="ai_image",
            video_script="一段足够长的脚本内容。" * 10,
        )
        fake_scene = _make_scene()
        with patch.object(sb, "generate_storyboard", return_value=[fake_scene]), \
             patch.object(m, "download_images_ai", return_value=[]), \
             patch.object(
                 m, "download_videos", return_value=["/tmp/pexels_fallback.mp4"]
             ) as pexels:
            result = task_mod.get_video_materials(
                "task-id", params, video_terms=["warehouse"], audio_duration=30.0
            )
        assert result == ["/tmp/pexels_fallback.mp4"]
        pexels.assert_called_once()
        assert pexels.call_args.kwargs["source"] == "pexels"


# ----------------------------------------------------------------------
# VideoParams 新字段
# ----------------------------------------------------------------------


class TestVideoParamsAIImageFields:
    def test_defaults(self):
        p = VideoParams(video_subject="x")
        assert p.image_provider == ""
        assert p.image_model == ""
        assert p.image_n_candidates == 1
        assert p.image_size == "1024x1024"

    def test_n_candidates_range(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VideoParams(video_subject="x", image_n_candidates=0)
        with pytest.raises(ValidationError):
            VideoParams(video_subject="x", image_n_candidates=9)

    def test_provider_max_length(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VideoParams(video_subject="x", image_provider="x" * 50)
