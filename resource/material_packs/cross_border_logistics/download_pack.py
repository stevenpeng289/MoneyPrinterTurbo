"""跨境物流主题包下载脚本。

用法：
  export PEXELS_API_KEY="your_key"
  uv run python resource/material_packs/cross_border_logistics/download_pack.py

可选参数：
  --per-term N      每个搜索词下几个（默认 1）
  --dry-run         只列出要下的视频，不实际下载

设计：
  - 复用 `app.services.material.search_videos_pexels`，不重写下载逻辑。
  - 失败的搜索词单独 log，不阻塞其他词。
  - 已存在同名 .mp4 自动跳过（用 size > 0 判断）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

# 让脚本能 import app/
THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import config  # noqa: E402
from app.models.schema import VideoAspect  # noqa: E402
from app.services.material import (  # noqa: E402
    save_video,
    search_videos_pexels,
)


def load_metadata() -> dict:
    meta_path = THIS_DIR / "metadata.yaml"
    with open(meta_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Download cross-border logistics material pack from Pexels.")
    parser.add_argument("--per-term", type=int, default=1, help="Videos per search term (default 1)")
    parser.add_argument("--dry-run", action="store_true", help="Only list what would be downloaded")
    args = parser.parse_args()

    if args.per_term < 1 or args.per_term > 5:
        parser.error("per-term must be in [1, 5]")

    api_keys = config.app.get("pexels_api_keys") or []
    if not api_keys:
        api_keys = [os.environ.get("PEXELS_API_KEY", "")]
    api_keys = [k for k in api_keys if k]
    if not api_keys:
        print("ERROR: no pexels_api_keys in config.toml and PEXELS_API_KEY env not set", file=sys.stderr)
        return 1

    meta = load_metadata()
    terms = meta.get("search_terms", [])
    print(f"[pack] {len(terms)} search terms, per-term={args.per_term}, dry-run={args.dry_run}")
    if not terms:
        print("ERROR: metadata.yaml has no search_terms", file=sys.stderr)
        return 1

    saved = 0
    skipped = 0
    failed: list[str] = []

    for term in terms:
        try:
            results = search_videos_pexels(
                search_term=term,
                minimum_duration=3,
                video_aspect=VideoAspect.portrait,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[pack] search '{term}' failed: {exc}")
            failed.append(term)
            continue

        if not results:
            print(f"[pack] no result for '{term}'")
            continue

        for idx, mat in enumerate(results[: args.per_term], start=1):
            target_name = f"{term.replace(' ', '_')}_{idx}.mp4"
            target = THIS_DIR / target_name
            if target.exists() and target.stat().st_size > 0:
                skipped += 1
                continue
            if args.dry_run:
                print(f"[pack] DRY: would save {target.name} from {mat.url}")
                saved += 1
                continue
            try:
                path = save_video(mat.url, save_dir=str(THIS_DIR))
                if path:
                    # save_video 用自己的命名；这里挪到 target_name
                    if Path(path).resolve() != target.resolve():
                        Path(path).rename(target)
                    saved += 1
                    print(f"[pack] saved {target.name}")
                else:
                    failed.append(term)
            except Exception as exc:  # noqa: BLE001
                print(f"[pack] save '{term}' #{idx} failed: {exc}")
                failed.append(term)

    print(f"\n[pack] done. saved={saved} skipped={skipped} failed={len(failed)}")
    if failed:
        print(f"[pack] failed terms: {sorted(set(failed))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
