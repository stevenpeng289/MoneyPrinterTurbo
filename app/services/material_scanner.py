"""素材目录增量扫描 + 自动打标编排（改造 D-2）。

工作流：
  扫 `material_directory` 下的 .mp4 →
  对每个文件比较 mtime 与 cache，新文件或被修改的才打标 →
  调 `auto_tagger.tag_video` →
  更新 cache。

设计要点：
- cache 文件 `.material_scan_cache.json` 落在 base_dir 根目录。
- 跳过已 lock 的 yaml 不算"成功打标"，但记到 cache 防反复重试。
- 路径白名单走 `file_security.resolve_path_within_directory`。
- `MaterialScanReport` 是 frozen dataclass，调用方禁止变更。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

from app.services.auto_tagger import (
    AutoTagError,
    AutoTagResult,
    TagCaller,
    tag_video,
)
from app.utils.file_security import resolve_path_within_directory


CACHE_FILENAME = ".material_scan_cache.json"
SUPPORTED_VIDEO_EXT = {".mp4"}


# ---------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------
@dataclass(frozen=True)
class MaterialScanReport:
    scanned: int
    tagged: int
    skipped_locked: int
    skipped_unchanged: int
    failed: int
    failed_paths: Tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "tagged": self.tagged,
            "skipped_locked": self.skipped_locked,
            "skipped_unchanged": self.skipped_unchanged,
            "failed": self.failed,
            "failed_paths": list(self.failed_paths),
        }


# ---------------------------------------------------------------
# cache 读写
# ---------------------------------------------------------------
def _cache_path(base_dir: str) -> str:
    return os.path.join(os.path.realpath(base_dir), CACHE_FILENAME)


def _load_cache(base_dir: str) -> Dict[str, float]:
    """加载 cache。返回 {abs_video_path: mtime_at_last_scan}。"""
    cp = _cache_path(base_dir)
    if not os.path.isfile(cp):
        return {}
    try:
        with open(cp, "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"material_scanner: cache load failed: {exc}, starting fresh")
        return {}


def _save_cache(base_dir: str, cache: Dict[str, float]) -> None:
    cp = _cache_path(base_dir)
    try:
        with open(cp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.warning(f"material_scanner: cache save failed: {exc}")


# ---------------------------------------------------------------
# 扫描入口
# ---------------------------------------------------------------
def list_video_files(base_dir: str) -> List[str]:
    """递归列出 base_dir 下所有支持的视频文件（绝对路径）。"""
    base_dir_real = os.path.realpath(base_dir)
    if not os.path.isdir(base_dir_real):
        return []
    out: List[str] = []
    for root, _, files in os.walk(base_dir_real):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SUPPORTED_VIDEO_EXT:
                continue
            out.append(os.path.abspath(os.path.join(root, fn)))
    return sorted(out)


def scan_and_tag(
    base_dir: str,
    tag_caller: TagCaller,
    *,
    overwrite: bool = False,
    num_frames: int = 3,
    model_label: str = "",
    max_videos: Optional[int] = None,
) -> MaterialScanReport:
    """扫 base_dir、按 mtime 增量打标。

    参数：
        base_dir:     素材根目录。
        tag_caller:   多模态 LLM 调用 (system, user, frames) -> raw。
        overwrite:    True = 重新打标所有视频（哪怕 cache 命中）。
        num_frames:   每个视频抽几帧。
        model_label:  yaml 里记录的 model 标识。
        max_videos:   本次最多处理几个（None = 无限制），便于批量任务限流。
    """
    base_dir_real = os.path.realpath(base_dir)
    if not os.path.isdir(base_dir_real):
        raise ValueError(f"base_dir is not a directory: {base_dir}")

    videos = list_video_files(base_dir_real)
    cache = _load_cache(base_dir_real)

    scanned = 0
    tagged = 0
    skipped_locked = 0
    skipped_unchanged = 0
    failed = 0
    failed_paths: List[str] = []

    for video_path in videos:
        if max_videos is not None and tagged + failed >= max_videos:
            logger.info(
                f"material_scanner: hit max_videos={max_videos}, stop early"
            )
            break

        scanned += 1
        # 增量判断：mtime 未变 + 不强制 overwrite + yaml 已存在 → 跳过
        try:
            mtime = os.path.getmtime(video_path)
        except OSError:
            failed += 1
            failed_paths.append(video_path)
            continue

        prev_mtime = cache.get(video_path)
        yaml_path = os.path.splitext(video_path)[0] + ".yaml"
        unchanged = (
            prev_mtime is not None
            and abs(prev_mtime - mtime) < 0.001
            and os.path.isfile(yaml_path)
            and not overwrite
        )
        if unchanged:
            skipped_unchanged += 1
            continue

        try:
            result: AutoTagResult = tag_video(
                video_path=video_path,
                base_dir=base_dir_real,
                tag_caller=tag_caller,
                num_frames=num_frames,
                overwrite=overwrite,
                model_label=model_label,
            )
        except (AutoTagError, ValueError) as exc:
            failed += 1
            failed_paths.append(video_path)
            logger.warning(
                f"material_scanner: tag_video failed for {video_path}: {exc}"
            )
            continue

        if result.skipped_locked:
            skipped_locked += 1
        else:
            tagged += 1

        # 写 cache（无论 lock 与否，避免下次反复重试 locked 文件）
        cache[video_path] = mtime

    _save_cache(base_dir_real, cache)

    report = MaterialScanReport(
        scanned=scanned,
        tagged=tagged,
        skipped_locked=skipped_locked,
        skipped_unchanged=skipped_unchanged,
        failed=failed,
        failed_paths=tuple(failed_paths),
    )
    logger.info(f"material_scanner: report={report.as_dict()}")
    return report


__all__ = [
    "CACHE_FILENAME",
    "MaterialScanReport",
    "list_video_files",
    "scan_and_tag",
]
