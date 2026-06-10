"""改造 D 测试：auto_tagger + material_scanner + material controller。

覆盖：
- parse_tags 容错（fence / noise / dedup / 错误）
- _pick_timestamps 边界
- write/read yaml 兼容 search_videos_local 格式
- tag_video lock 文件保护
- tag_video 已有 yaml 不覆盖 (overwrite=False)
- material_scanner.scan_and_tag 增量缓存 / mtime 变化触发重打标
- /api/v1/material-packs 列表
- /api/v1/material-scan 启停 / 越界 / 成功
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi.testclient import TestClient

from app.asgi import app
from app.config import config
from app.services import auto_tagger as at
from app.services import material_scanner as ms


# ----------------------------------------------------------------------
# parse_tags
# ----------------------------------------------------------------------


class TestParseTags:
    def test_plain_json(self):
        assert at.parse_tags('["a","b","c"]') == ("a", "b", "c")

    def test_lower_case_normalization(self):
        assert at.parse_tags('["Warehouse", "FORKLIFT"]') == (
            "warehouse",
            "forklift",
        )

    def test_dedup(self):
        assert at.parse_tags('["a","A","a ","b"]') == ("a", "b")

    def test_fenced_json(self):
        assert at.parse_tags('```json\n["x","y"]\n```') == ("x", "y")

    def test_with_noise(self):
        assert at.parse_tags('Sure: ["p","q"] enjoy') == ("p", "q")

    def test_empty_response_raises(self):
        with pytest.raises(at.TagParseError, match="empty"):
            at.parse_tags("")

    def test_non_array_raises(self):
        with pytest.raises(at.TagParseError, match="expected array"):
            at.parse_tags('{"tags": ["a"]}')

    def test_invalid_json_raises(self):
        with pytest.raises(at.TagParseError):
            at.parse_tags("not json at all")

    def test_reasoning_model_think_block_stripped(self):
        """minimax-M2.7 / deepseek-r1 等 reasoning model 会在 JSON 前输出
        <think>...</think> 块。parse_tags 必须先剥掉再 json.loads。
        """
        raw = (
            '<think>The user wants me to output JSON. Let me think...'
            "I'll list warehouse, forklift, logistics.</think>\n\n"
            '["warehouse", "forklift", "logistics"]'
        )
        assert at.parse_tags(raw) == ("warehouse", "forklift", "logistics")

    def test_reasoning_model_think_block_with_fence(self):
        """think 块 + markdown 围栏混合也要剥干净。"""
        raw = (
            "<think>reasoning here</think>\n"
            "```json\n"
            '["cargo box", "shipping"]\n'
            "```"
        )
        assert at.parse_tags(raw) == ("cargo box", "shipping")

    def test_no_valid_tags_raises(self):
        with pytest.raises(at.TagParseError, match="no valid"):
            at.parse_tags('["", "  ", null]')


# ----------------------------------------------------------------------
# timestamp 计算
# ----------------------------------------------------------------------


class TestPickTimestamps:
    def test_zero_frames(self):
        assert at._pick_timestamps(100.0, 0) == []

    def test_single_frame_midpoint(self):
        assert at._pick_timestamps(60.0, 1) == [30.0]

    def test_three_frames_evenly_spaced(self):
        ts = at._pick_timestamps(100.0, 3)
        assert len(ts) == 3
        assert ts[0] == pytest.approx(5.0)
        assert ts[-1] == pytest.approx(95.0)
        # 中间点应在 50 附近
        assert 49.0 < ts[1] < 51.0

    def test_six_frames(self):
        ts = at._pick_timestamps(60.0, 6)
        assert len(ts) == 6
        assert ts == sorted(ts)


# ----------------------------------------------------------------------
# yaml 兼容性（与 search_videos_local 读取格式一致）
# ----------------------------------------------------------------------


class TestYamlCompatibility:
    def test_write_read_roundtrip(self, tmp_path):
        yaml_path = tmp_path / "v.yaml"
        at.write_tags_yaml(
            str(yaml_path),
            tags=("cargo", "warehouse"),
            source="auto_tagger",
            model="openai/gpt-4o-mini",
        )
        with open(yaml_path, "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f)
        assert meta["tags"] == ["cargo", "warehouse"]
        assert meta["source"] == "auto_tagger"
        assert meta["model"] == "openai/gpt-4o-mini"

    def test_read_existing_returns_empty_when_missing(self, tmp_path):
        nope = tmp_path / "nope.yaml"
        assert at._read_existing_tags(str(nope)) == tuple()

    def test_read_existing_returns_tuple(self, tmp_path):
        yaml_path = tmp_path / "x.yaml"
        yaml_path.write_text("tags:\n  - a\n  - B\n  - ' c '\n")
        assert at._read_existing_tags(str(yaml_path)) == ("a", "b", "c")


# ----------------------------------------------------------------------
# tag_video lock 保护
# ----------------------------------------------------------------------


class TestTagVideoLockProtection:
    def test_locked_skips_overwrite(self, tmp_path):
        # 创建假视频文件 + 假 yaml + lock
        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"\x00\x00\x00")
        yaml_path = tmp_path / "v.yaml"
        yaml_path.write_text("tags:\n  - manual\n")
        lock_path = tmp_path / "v.yaml.locked"
        lock_path.write_text("")

        def caller(s, u, frames):
            raise AssertionError("should not be called when locked")

        result = at.tag_video(
            video_path=str(vid),
            base_dir=str(tmp_path),
            tag_caller=caller,
        )
        assert result.skipped_locked is True
        assert result.tags == ("manual",)
        assert result.frames_extracted == 0

    def test_existing_yaml_without_overwrite_skips(self, tmp_path):
        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"\x00")
        yaml_path = tmp_path / "v.yaml"
        yaml_path.write_text("tags:\n  - existing\n")

        def caller(s, u, frames):
            raise AssertionError("should not be called when yaml exists")

        result = at.tag_video(
            video_path=str(vid),
            base_dir=str(tmp_path),
            tag_caller=caller,
            overwrite=False,
        )
        assert result.skipped_locked is False
        assert result.tags == ("existing",)
        assert result.frames_extracted == 0


# ----------------------------------------------------------------------
# material_scanner 增量
# ----------------------------------------------------------------------


class TestMaterialScannerIncremental:
    def test_empty_dir(self, tmp_path):
        report = ms.scan_and_tag(str(tmp_path), tag_caller=lambda s, u, f: '["x"]')
        assert report.scanned == 0
        assert report.tagged == 0

    def test_skipped_unchanged_when_mtime_same(self, tmp_path):
        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"\x00")
        yaml_path = tmp_path / "v.yaml"
        yaml_path.write_text("tags:\n  - existing\n")

        # 第一次跑：会走 "已有 yaml + overwrite=False" 路径，tagged=0 (because tag_video returns skipped_unchanged-like state)
        # 但 cache 不存在 → unchanged 判断只用 cache，第一次写入 cache
        with patch.object(at, "extract_frames"):
            report1 = ms.scan_and_tag(
                str(tmp_path), tag_caller=lambda s, u, f: '["x"]'
            )
        assert report1.scanned == 1

        # 第二次跑：cache 命中 + yaml 存在 + mtime 未变 → unchanged
        report2 = ms.scan_and_tag(
            str(tmp_path), tag_caller=lambda s, u, f: '["x"]'
        )
        assert report2.skipped_unchanged == 1

    def test_overwrite_triggers_retag(self, tmp_path):
        # 拓扑文件 + 已有 yaml；overwrite=True 应该重新调 tag_video
        vid = tmp_path / "v.mp4"
        vid.write_bytes(b"\x00")
        yaml_path = tmp_path / "v.yaml"
        yaml_path.write_text("tags:\n  - old\n")

        called = []

        def fake_tag_video(**kwargs):
            called.append(kwargs)
            return at.AutoTagResult(
                video_path=kwargs["video_path"],
                yaml_path=str(yaml_path),
                tags=("new",),
                frames_extracted=3,
                skipped_locked=False,
            )

        with patch.object(ms, "tag_video", side_effect=fake_tag_video):
            report = ms.scan_and_tag(
                str(tmp_path),
                tag_caller=lambda s, u, f: '["new"]',
                overwrite=True,
            )
        assert report.tagged == 1
        assert len(called) == 1

    def test_max_videos_limits(self, tmp_path):
        for i in range(5):
            (tmp_path / f"v{i}.mp4").write_bytes(b"\x00")

        called = []

        def fake_tag_video(**kwargs):
            called.append(kwargs["video_path"])
            return at.AutoTagResult(
                video_path=kwargs["video_path"],
                yaml_path=kwargs["video_path"] + ".yaml",
                tags=("x",),
                frames_extracted=1,
                skipped_locked=False,
            )

        with patch.object(ms, "tag_video", side_effect=fake_tag_video):
            report = ms.scan_and_tag(
                str(tmp_path),
                tag_caller=lambda s, u, f: '["x"]',
                max_videos=2,
            )
        assert report.tagged == 2
        # max_videos 限制后剩余文件不会被计入 scanned
        assert len(called) == 2


# ----------------------------------------------------------------------
# FastAPI 端到端
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


class TestMaterialPacksEndpoint:
    def test_list_packs(self, client: TestClient):
        r = client.get("/api/v1/material-packs")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["count"] >= 1
        # 内置主题包应包含 cross_border_logistics
        ids = [p["id"] for p in data["packs"]]
        assert "cross_border_logistics" in ids
        pack = next(p for p in data["packs"] if p["id"] == "cross_border_logistics")
        assert pack["search_term_count"] > 0
        assert pack["category"] == "industry"


class TestMaterialScanEndpoint:
    def test_disabled_returns_400(self, client: TestClient):
        # auto_tagger_enabled=false（默认）→ 400
        r = client.post(
            "/api/v1/material-scan",
            json={
                "base_dir": "resource/material_packs/cross_border_logistics"
            },
        )
        assert r.status_code == 400

    def test_path_traversal_blocked(self, client: TestClient):
        # 即使临时 enable，也要确认越界路径被拦
        with patch.dict(config.app, {"auto_tagger_enabled": True}):
            r = client.post(
                "/api/v1/material-scan", json={"base_dir": "/etc"}
            )
        assert r.status_code == 400

    def test_valid_scan_returns_report(self, client: TestClient, tmp_path):
        # 设 material_directory = tmp_path 并 enable
        with patch.dict(
            config.app,
            {
                "auto_tagger_enabled": True,
                "material_directory": str(tmp_path),
            },
        ), patch(
            "app.services.llm_multimodal.get_default_tag_caller",
            return_value=lambda s, u, f: '["x"]',
        ):
            r = client.post(
                "/api/v1/material-scan",
                json={
                    "base_dir": str(tmp_path),
                    "max_videos": 10,
                },
            )
        assert r.status_code == 200
        rep = r.json()["data"]
        assert rep["scanned"] == 0  # 空目录
        assert rep["tagged"] == 0
