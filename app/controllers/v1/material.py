"""素材库智能化 controller（改造 D）。

接口：
    GET  /api/v1/material-packs        列出内置主题包
    POST /api/v1/material-scan         触发自动打标扫描
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from fastapi import HTTPException, Request
from loguru import logger

from app.controllers.v1.base import new_router
from app.models.schema import (
    MaterialPackInfo,
    MaterialPackListResponse,
    MaterialScanRequest,
    MaterialScanResponse,
)
from app.config import config
from app.services import material_scanner
from app.services.llm_multimodal import get_default_tag_caller
from app.utils import utils
from app.utils.file_security import resolve_path_within_directory

router = new_router()


# 项目根：从 app/controllers/v1/material.py 反推三层。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PACKS_DIR = _PROJECT_ROOT / "resource" / "material_packs"


def _read_pack(pack_dir: Path) -> MaterialPackInfo | None:
    meta_path = pack_dir / "metadata.yaml"
    if not meta_path.is_file():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"material_packs: failed to read {meta_path}: {exc}")
        return None
    if not isinstance(meta, dict):
        return None
    pack_id = str(meta.get("id") or pack_dir.name)
    return MaterialPackInfo(
        id=pack_id,
        name=str(meta.get("name", pack_id)),
        description=str(meta.get("description", "")),
        category=str(meta.get("category", "industry")),
        language=str(meta.get("language", "en")),
        recommended_clip_duration=int(meta.get("recommended_clip_duration", 5)),
        search_term_count=len(meta.get("search_terms") or []),
        path=str(pack_dir.relative_to(_PROJECT_ROOT)),
    )


@router.get(
    "/material-packs",
    response_model=MaterialPackListResponse,
    summary="List built-in material packs (改造 D)",
)
def list_material_packs(request: Request):
    """列出 resource/material_packs/ 下所有含 metadata.yaml 的目录。"""
    packs: list[dict] = []
    if _PACKS_DIR.is_dir():
        for sub in sorted(_PACKS_DIR.iterdir()):
            if not sub.is_dir():
                continue
            info = _read_pack(sub)
            if info is not None:
                packs.append(info.model_dump())
    return utils.get_response(200, {"packs": packs, "count": len(packs)})


def _resolve_scan_base_dir(raw: str) -> str:
    """把用户传入的 base_dir 解析成绝对路径，并做白名单校验。

    允许两类目录：
    1. `resource/material_packs/<x>`：项目内置主题包。
    2. `material_directory` 配置指向的目录或其子目录。

    其他路径一律拒绝（防 path traversal）。
    """
    abs_root = str(_PROJECT_ROOT)
    # 先试作为项目相对路径
    try:
        resolved = resolve_path_within_directory(
            abs_root, raw, require_file=False
        )
    except ValueError:
        resolved = None

    # 再试 material_directory 内
    material_dir = (config.app.get("material_directory") or "").strip()
    if material_dir and material_dir != "task" and os.path.isdir(material_dir):
        try:
            resolved_md = resolve_path_within_directory(
                material_dir, raw, require_file=False
            )
        except ValueError:
            resolved_md = None
        if resolved_md is not None and (resolved is None or resolved != resolved_md):
            resolved = resolved_md

    if resolved is None or not os.path.isdir(resolved):
        raise HTTPException(
            status_code=400,
            detail=f"base_dir invalid or outside allowed paths: {raw!r}",
        )
    return resolved


@router.post(
    "/material-scan",
    response_model=MaterialScanResponse,
    summary="Scan a material directory and auto-tag videos with multimodal LLM (改造 D)",
)
def scan_material_directory(request: Request, body: MaterialScanRequest):
    """触发素材目录扫描 + 自动打标。

    错误码：
      400 — base_dir 越界或不存在
      502 — 多模态 LLM 不可用或全部 tag 调用都失败
    """
    if not config.app.get("auto_tagger_enabled", False):
        raise HTTPException(
            status_code=400,
            detail="auto_tagger_enabled=false in config.toml; please enable it first",
        )

    safe_base = _resolve_scan_base_dir(body.base_dir)
    tag_caller = get_default_tag_caller()

    try:
        report = material_scanner.scan_and_tag(
            base_dir=safe_base,
            tag_caller=tag_caller,
            overwrite=body.overwrite,
            num_frames=body.num_frames,
            model_label=body.model_label,
            max_videos=body.max_videos,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(f"material-scan failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return utils.get_response(200, report.as_dict())
