"""AI 图像生成抽象层（改造 C-1）。

设计目标：
- 统一接口：`generate_image(prompt, n, reference_images)` → `list[GeneratedImage]`。
- 当前实现：OpenAI 兼容 `/images/generations`（MiniMax / OpenAI / aihubmix 等）。
- 留扩展点：doubao（即梦）、kling（可灵）、本地 SD。
- 失败重试 + 详细错误分级，便于 task 层做 Pexels 降级。

注意：
- 用户配置 `image_provider` 选择具体 provider；缺失时默认 OpenAI 兼容协议。
- `reference_images` 为 image-to-image 用，OpenAI 标准 `/images/generations`
  不支持参考图，所以在该 provider 下 reference_images 被忽略（log warning）。
  doubao / kling 等支持参考图时各自重写。
"""

from __future__ import annotations

import base64
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional, Protocol, runtime_checkable

import requests
from loguru import logger
from openai import OpenAI

from app.config import config


# ---------------------------------------------------------------
# 错误体系
# ---------------------------------------------------------------
class AIImageError(Exception):
    """ai_image 统一错误基类。"""


class ImageProviderUnavailableError(AIImageError):
    """provider 缺 api key / 不支持 / 包未装。"""


class ImageGenerationError(AIImageError):
    """生成阶段失败（API 调用错误、超时等）。"""


# ---------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------
@dataclass(frozen=True)
class GeneratedImage:
    local_path: str           # 必填，下载/解码后的本地文件路径
    prompt: str
    provider: str
    url: str = ""             # 远端 URL（如果有）
    seed: Optional[int] = None
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------
# Provider 协议
# ---------------------------------------------------------------
@runtime_checkable
class ImageProvider(Protocol):
    """所有 image provider 必须实现此接口。"""

    name: str

    def generate(
        self,
        prompt: str,
        *,
        n: int = 1,
        reference_images: Optional[List[str]] = None,
        size: str = "1024x1024",
        output_dir: Optional[str] = None,
    ) -> List[GeneratedImage]:
        ...


# ---------------------------------------------------------------
# OpenAI 兼容 provider（默认实现）
# ---------------------------------------------------------------
class OpenAICompatibleImageProvider:
    """OpenAI `/images/generations` 兼容 provider。

    支持：openai / aihubmix / minimax（MiniMax）/ 其他 OpenAI 兼容平台。
    """

    def __init__(self, provider_key: str):
        self.name = provider_key
        # 别名映射：provider 名 → config.toml 前缀
        # 例：'minimax' 在 config.toml 里实际是 'minimax_*' (历史命名)
        config_prefix = _PROVIDER_CONFIG_PREFIX.get(provider_key, provider_key)
        api_key = config.app.get(f"{config_prefix}_api_key", "")
        base_url = config.app.get(f"{config_prefix}_base_url", "") or None
        # image model 名独立配置，避免和 chat model 混用
        self.model = config.app.get(
            f"{config_prefix}_image_model_name", ""
        ) or config.app.get(f"{config_prefix}_model_name", "dall-e-3")
        if not api_key:
            raise ImageProviderUnavailableError(
                f"image provider '{provider_key}' missing api key "
                f"(config: {config_prefix}_api_key)"
            )
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def generate(
        self,
        prompt: str,
        *,
        n: int = 1,
        reference_images: Optional[List[str]] = None,
        size: str = "1024x1024",
        output_dir: Optional[str] = None,
    ) -> List[GeneratedImage]:
        if reference_images:
            logger.warning(
                "ai_image: OpenAI /images/generations 不支持 reference_images，已忽略"
            )
        if n < 1 or n > 10:
            raise ValueError(f"n must be in [1, 10], got {n}")

        try:
            response = self.client.images.generate(
                model=self.model,
                prompt=prompt,
                n=n,
                size=size,
                response_format="b64_json",
            )
        except Exception as exc:  # noqa: BLE001
            raise ImageGenerationError(
                f"{self.name}: image API call failed: {exc}"
            ) from exc

        data = getattr(response, "data", None) or []
        if not data:
            raise ImageGenerationError(f"{self.name}: empty image data")

        target_dir = output_dir or tempfile.mkdtemp(prefix="ai_image_")
        os.makedirs(target_dir, exist_ok=True)
        results: List[GeneratedImage] = []
        for idx, item in enumerate(data, start=1):
            local_path = _persist_image(
                item, target_dir, basename=f"{self.name}_{int(time.time())}_{idx}"
            )
            results.append(
                GeneratedImage(
                    local_path=local_path,
                    prompt=prompt,
                    provider=self.name,
                    url=getattr(item, "url", "") or "",
                )
            )
        return results


def _persist_image(item, output_dir: str, basename: str) -> str:
    """把 OpenAI image 返回的 b64_json 或 url 落盘成 jpg/png。"""
    out_path = os.path.join(output_dir, f"{basename}.png")
    b64 = getattr(item, "b64_json", None)
    if b64:
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64))
        return out_path
    url = getattr(item, "url", None)
    if url:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(resp.content)
        return out_path
    raise ImageGenerationError(
        "image item has neither b64_json nor url"
    )


# ---------------------------------------------------------------
# MiniMax 图像 provider (POST /v1/image_generation, 私有 schema)
# ---------------------------------------------------------------
class MinimaxImageProvider:
    """Minimax 图像生成 provider。

    端点：`POST {base_url}/image_generation`（注意单数，不是 OpenAI 的 /images/generations）
    模型：`image-01`
    请求私有字段：`aspect_ratio` / `subject_reference` / `response_format=base64`
    响应：`{"data": {"image_base64": [str, ...]}}`

    aspect_ratio 自动从 OpenAI 风格 size 推断：
      1024x1024 → "1:1"
      1024x1792 → "9:16"（portrait）
      1792x1024 → "16:9"（landscape）
    """

    DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
    name = "minimax"

    def __init__(self, provider_key: str = "minimax"):
        self.name = provider_key
        config_prefix = _PROVIDER_CONFIG_PREFIX.get(provider_key, provider_key)
        self.api_key = config.app.get(f"{config_prefix}_api_key", "")
        if not self.api_key:
            raise ImageProviderUnavailableError(
                f"image provider '{provider_key}' missing api key "
                f"(config: {config_prefix}_api_key)"
            )
        self.base_url = (
            config.app.get(f"{config_prefix}_base_url", "")
            or self.DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = (
            config.app.get(f"{config_prefix}_image_model_name", "") or "image-01"
        )

    def _size_to_aspect_ratio(self, size: str) -> str:
        try:
            w, h = (int(x) for x in size.lower().split("x"))
        except (ValueError, AttributeError):
            return "1:1"
        if w == h:
            return "1:1"
        if w > h:
            return "16:9"
        return "9:16"

    def generate(
        self,
        prompt: str,
        *,
        n: int = 1,
        reference_images: Optional[List[str]] = None,
        size: str = "1024x1024",
        output_dir: Optional[str] = None,
    ) -> List[GeneratedImage]:
        if n < 1 or n > 9:
            raise ValueError(f"minimax n must be in [1, 9], got {n}")

        body = {
            "model": self.model,
            "prompt": prompt,
            "aspect_ratio": self._size_to_aspect_ratio(size),
            "response_format": "base64",
            "n": n,
        }
        if reference_images:
            body["subject_reference"] = [
                {"type": "character", "image_file": ref}
                for ref in reference_images
            ]

        endpoint = f"{self.base_url}/image_generation"
        try:
            resp = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=120,
            )
        except requests.RequestException as exc:
            raise ImageGenerationError(
                f"{self.name}: HTTP request failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise ImageGenerationError(
                f"{self.name}: HTTP {resp.status_code}: {resp.text[:300]}"
            )

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ImageGenerationError(
                f"{self.name}: response not JSON: {exc}"
            ) from exc

        # Minimax 业务错误也是 200 + base_resp.status_code != 0
        base_resp = payload.get("base_resp") or {}
        biz_code = base_resp.get("status_code", 0)
        if biz_code not in (0, None):
            raise ImageGenerationError(
                f"{self.name}: biz error {biz_code}: "
                f"{base_resp.get('status_msg', '')[:200]}"
            )

        data = payload.get("data") or {}
        b64_list = data.get("image_base64") or []
        if not b64_list:
            # 兼容 image_urls 备用通道
            urls = data.get("image_urls") or []
            if not urls:
                raise ImageGenerationError(
                    f"{self.name}: empty image_base64 and image_urls"
                )
            return self._persist_urls(urls, prompt, output_dir)

        return self._persist_b64(b64_list, prompt, output_dir)

    def _persist_b64(
        self, b64_list: list, prompt: str, output_dir: Optional[str]
    ) -> List[GeneratedImage]:
        target_dir = output_dir or tempfile.mkdtemp(prefix="ai_image_")
        os.makedirs(target_dir, exist_ok=True)
        stamp = int(time.time())
        results: List[GeneratedImage] = []
        for idx, b64 in enumerate(b64_list, start=1):
            out_path = os.path.join(
                target_dir, f"{self.name}_{stamp}_{idx}.png"
            )
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            results.append(
                GeneratedImage(
                    local_path=out_path,
                    prompt=prompt,
                    provider=self.name,
                )
            )
        return results

    def _persist_urls(
        self, urls: list, prompt: str, output_dir: Optional[str]
    ) -> List[GeneratedImage]:
        target_dir = output_dir or tempfile.mkdtemp(prefix="ai_image_")
        os.makedirs(target_dir, exist_ok=True)
        stamp = int(time.time())
        results: List[GeneratedImage] = []
        for idx, url in enumerate(urls, start=1):
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            out_path = os.path.join(
                target_dir, f"{self.name}_{stamp}_{idx}.png"
            )
            with open(out_path, "wb") as f:
                f.write(r.content)
            results.append(
                GeneratedImage(
                    local_path=out_path,
                    prompt=prompt,
                    provider=self.name,
                    url=url,
                )
            )
        return results


# ---------------------------------------------------------------
# Provider 注册表
# ---------------------------------------------------------------
_OPENAI_COMPATIBLE_PROVIDERS = {
    "openai",
    "aihubmix",
    "minimax",  # MiniMax
    "oneapi",
    "moonshot",
    "deepseek",
    "groq",
}

# Provider 名 → config.toml 前缀映射
# config.toml 里 MiniMax 历史命名是 'minimax_*'（platform.minimaxi.com 来源），
# 但代码层统一用 'minimax' 标识。这里桥接。
_PROVIDER_CONFIG_PREFIX = {
    "minimax": "minimax",
}


def get_provider(provider_key: str = "") -> ImageProvider:
    """获取指定 provider 实例；缺省时读 config.app.image_provider。"""
    key = (provider_key or config.app.get("image_provider", "") or "").strip()
    if not key:
        # 默认跟着 llm_provider 走，但要求是 OpenAI 兼容协议的
        key = (config.app.get("llm_provider", "openai") or "openai").strip()

    # minimax 用私有协议，单独路由
    if key == "minimax":
        return MinimaxImageProvider(key)

    if key in _OPENAI_COMPATIBLE_PROVIDERS:
        return OpenAICompatibleImageProvider(key)

    raise ImageProviderUnavailableError(
        f"image provider '{key}' not supported yet (supported: {sorted(_OPENAI_COMPATIBLE_PROVIDERS)})"
    )


def generate_image(
    prompt: str,
    *,
    n: int = 1,
    reference_images: Optional[List[str]] = None,
    size: str = "1024x1024",
    output_dir: Optional[str] = None,
    provider_key: str = "",
) -> List[GeneratedImage]:
    """门面函数：拿到 provider → 生成 → 返回结果列表。

    `reference_images` 在 OpenAI 兼容 provider 下被忽略；doubao/kling 接入后才生效。
    """
    provider = get_provider(provider_key)
    return provider.generate(
        prompt=prompt,
        n=n,
        reference_images=reference_images,
        size=size,
        output_dir=output_dir,
    )


__all__ = [
    "AIImageError",
    "GeneratedImage",
    "ImageGenerationError",
    "ImageProvider",
    "ImageProviderUnavailableError",
    "MinimaxImageProvider",
    "OpenAICompatibleImageProvider",
    "generate_image",
    "get_provider",
]
