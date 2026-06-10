import os
import random
import threading
from typing import List
from urllib.parse import urlencode

import requests
import yaml
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材搜索和下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def get_api_key(cfg_key: str):
    api_keys = config.app.get(cfg_key)
    if not api_keys:
        raise ValueError(
            f"\n\n##### {cfg_key} is not set #####\n\nPlease set it in the config.toml file: {config.config_file}\n\n"
            f"{utils.to_json(config.app)}"
        )

    # if only one key is provided, return it
    if isinstance(api_keys, str):
        return api_keys

    global _api_key_counter
    with _api_key_lock:
        _api_key_counter += 1
        return api_keys[_api_key_counter % len(api_keys)]


def search_videos_pexels(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)
    video_orientation = aspect.name
    video_width, video_height = aspect.to_resolution()
    api_key = get_api_key("pexels_api_keys")
    headers = {
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }
    # Build URL
    params = {"query": search_term, "per_page": 20, "orientation": video_orientation}
    query_url = f"https://api.pexels.com/videos/search?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url,
            headers=headers,
            proxies=config.proxy,
            verify=_get_tls_verify(),
            timeout=(30, 60),
        )
        response = r.json()
        video_items = []
        if "videos" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["videos"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["video_files"]
            # loop through each url to determine the best quality
            for video in video_files:
                w = int(video["width"])
                h = int(video["height"])
                if w == video_width and h == video_height:
                    item = MaterialInfo()
                    item.provider = "pexels"
                    item.url = video["link"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_pixabay(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    aspect = VideoAspect(video_aspect)

    video_width, video_height = aspect.to_resolution()

    api_key = get_api_key("pixabay_api_keys")
    # Build URL
    params = {
        "q": search_term,
        "video_type": "all",  # Accepted values: "all", "film", "animation"
        "per_page": 50,
        "key": api_key,
    }
    query_url = f"https://pixabay.com/api/videos/?{urlencode(params)}"
    logger.info(f"searching videos: {query_url}, with proxies: {config.proxy}")

    try:
        r = requests.get(
            query_url, proxies=config.proxy, verify=_get_tls_verify(), timeout=(30, 60)
        )
        response = r.json()
        video_items = []
        if "hits" not in response:
            logger.error(f"search videos failed: {response}")
            return video_items
        videos = response["hits"]
        # loop through each video in the result
        for v in videos:
            duration = v["duration"]
            # check if video has desired minimum duration
            if duration < minimum_duration:
                continue
            video_files = v["videos"]
            # loop through each url to determine the best quality
            for video_type in video_files:
                video = video_files[video_type]
                w = int(video["width"])
                # h = int(video["height"])
                if w >= video_width:
                    item = MaterialInfo()
                    item.provider = "pixabay"
                    item.url = video["url"]
                    item.duration = duration
                    video_items.append(item)
                    break
        return video_items
    except Exception as e:
        logger.error(f"search videos failed: {str(e)}")

    return []


def search_videos_local(
    search_term: str,
    minimum_duration: int,
    video_aspect: VideoAspect = VideoAspect.portrait,
) -> List[MaterialInfo]:
    """
    扫描 material_directory 下的本地视频，按 search_term 与同名 yaml 里的 tags
    做大小写无关的子串匹配，返回命中的 MaterialInfo 列表。

    每个本地视频需配同名 .yaml 描述文件，包含 tags 列表（英文 tag，跟 LLM 生成的
    video_terms 一致）。命中的视频以 file:// 协议返回，由 save_video 短路处理。
    """
    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task" or not os.path.isdir(material_directory):
        logger.warning(
            f"local_search: material_directory 无效或未设置: '{material_directory}', "
            f"跳过本次搜索"
        )
        return []

    search_term_lower = search_term.lower().strip()
    video_items: List[MaterialInfo] = []

    for root, _, files in os.walk(material_directory):
        for filename in files:
            if not filename.lower().endswith(".mp4"):
                continue
            video_path = os.path.abspath(os.path.join(root, filename))
            yaml_path = os.path.splitext(video_path)[0] + ".yaml"

            # 1. 读 yaml tags
            if not os.path.isfile(yaml_path):
                logger.debug(f"local_search: {filename} 无 yaml 描述, 跳过")
                continue
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    meta = yaml.safe_load(f) or {}
                tags = meta.get("tags") or []
                if not isinstance(tags, list):
                    logger.warning(f"local_search: {yaml_path} tags 不是 list, 跳过")
                    continue
            except Exception as e:
                logger.warning(f"local_search: 解析 {yaml_path} 失败: {e}, 跳过")
                continue

            # 2. 匹配 search_term（任一 tag 与 search_term 双向子串）
            hit = False
            for tag in tags:
                tag_str = str(tag).lower().strip()
                if not tag_str:
                    continue
                if search_term_lower in tag_str or tag_str in search_term_lower:
                    hit = True
                    break
            if not hit:
                continue

            # 3. 读真实 duration，过滤掉太短的视频
            clip = None
            try:
                clip = VideoFileClip(video_path)
                duration = clip.duration
            except Exception as e:
                logger.warning(f"local_search: 视频 {video_path} 损坏: {e}, 跳过")
                continue
            finally:
                if clip is not None:
                    try:
                        clip.close()
                    except Exception:
                        pass
            if duration < minimum_duration:
                logger.debug(
                    f"local_search: {filename} duration={duration:.1f}s "
                    f"< {minimum_duration}s, 跳过"
                )
                continue

            # 4. 构造 MaterialInfo（file:// 协议由 save_video 短路处理）
            item = MaterialInfo()
            item.provider = "local"
            item.url = f"file://{video_path}"
            item.duration = duration
            video_items.append(item)
            logger.info(
                f"local_search: 命中 {filename} (duration={duration:.1f}s, "
                f"search_term='{search_term}')"
            )

    return video_items


def save_video(video_url: str, save_dir: str = "") -> str:
    # 本地视频（file:// 协议）：直接返回绝对路径，不下载、不改名
    if video_url.startswith("file://"):
        local_path = video_url[len("file://"):]
        if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
            return local_path
        logger.warning(f"save_video: file:// 指向不存在的文件: {local_path}")
        return ""
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    with open(video_path, "wb") as f:
        f.write(
            requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
            ).content
        )

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "pexels",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_contact_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
) -> List[str]:
    valid_video_items = []
    valid_video_urls = []
    found_duration = 0.0
    search_videos = search_videos_pexels
    if source == "pixabay":
        search_videos = search_videos_pixabay
    elif source == "local_search":
        search_videos = search_videos_local

    for search_term in search_terms:
        video_items = search_videos(
            search_term=search_term,
            minimum_duration=max_clip_duration,
            video_aspect=video_aspect,
        )
        logger.info(f"found {len(video_items)} videos for '{search_term}'")

        for item in video_items:
            if item.url not in valid_video_urls:
                valid_video_items.append(item)
                valid_video_urls.append(item.url)
                found_duration += item.duration

    logger.info(
        f"found total videos: {len(valid_video_items)}, required duration: {audio_duration} seconds, found duration: {found_duration} seconds"
    )
    video_paths = []

    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    concat_mode_value = getattr(video_contact_mode, "value", video_contact_mode)
    if concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(valid_video_items)

    total_duration = 0.0
    for item in valid_video_items:
        try:
            logger.info(f"downloading video: {item.url}")
            saved_video_path = save_video(
                video_url=item.url, save_dir=material_directory
            )
            if saved_video_path:
                logger.info(f"video saved: {saved_video_path}")
                video_paths.append(saved_video_path)
                seconds = min(max_clip_duration, item.duration)
                total_duration += seconds
                if total_duration > audio_duration:
                    logger.info(
                        f"total duration of downloaded videos: {total_duration} seconds, skip downloading more"
                    )
                    break
        except Exception as e:
            logger.error(f"failed to download video: {utils.to_json(item)} => {str(e)}")
    logger.success(f"downloaded {len(video_paths)} videos")
    return video_paths


# ----------------------------------------------------------------------
# 改造 C：AI 出图（拆分镜 → 每分镜生成 N 张 → VLM 选最佳）
# ----------------------------------------------------------------------


def download_images_ai(
    task_id: str,
    scenes: list,
    *,
    audio_duration: float = 0.0,
    n_candidates: int = 1,
    image_provider: str = "",
    image_size: str = "1024x1024",
    select_best_caller=None,
) -> list[str]:
    """根据分镜列表用 AI 出图，返回本地图片路径列表（兼容 preprocess_video 图片分支）。

    Args:
        task_id:             用于落盘目录推断。
        scenes:              `storyboard.Scene` 列表（含 image_prompt / visual_desc / target_duration）。
        audio_duration:      累积时长上限（与 download_videos 一致）；达到即停。
        n_candidates:        每分镜生成几张候选。1 = 不调 VLM。
        image_provider:      传给 ai_image.get_provider；空字符串走默认。
        image_size:          OpenAI 图像 size 字符串，如 "1024x1024" / "1024x1792"。
        select_best_caller:  VLM caller，n_candidates>1 时用来从候选里选最佳。
                             为 None 时取 `llm_multimodal.get_default_tag_caller()`。

    失败的分镜不会抛错，仅 log，并跳过——保证整批流程不被单个分镜卡住。
    上层（task.py）应根据返回长度判断是否要 fallback 到 Pexels。
    """
    from app.services import ai_image, consistency_filter
    from app.services.llm_multimodal import get_default_tag_caller

    if not scenes:
        return []

    # AI 出图统一落到 `local_videos_dir`（与 `download_videos` 对齐），
    # 这样下游 `video.preprocess_video()` 的路径白名单能通过，不会出现
    # "ai_image 落盘 → preprocess 全部 skip → 0 materials"的链路断裂。
    # 历史曾用 `material_directory`，但 video.py 仅放行 `local_videos_dir`，
    # 导致 ai_image 模式与 local_search / pexels 模式混用同一目录时跑不通。
    material_directory = utils.storage_dir("local_videos", create=True)

    if select_best_caller is None and n_candidates > 1:
        try:
            select_best_caller = get_default_tag_caller()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"ai_image: no VLM caller available, will pick first candidate: {exc}"
            )
            select_best_caller = None

    image_paths: list[str] = []
    accumulated = 0.0

    for scene in scenes:
        prompt = getattr(scene, "image_prompt", "") or ""
        visual_desc = getattr(scene, "visual_desc", "") or prompt
        target_duration = float(getattr(scene, "target_duration", 5.0))
        if not prompt:
            logger.warning(f"ai_image: scene {getattr(scene, 'scene_id', '?')} has empty image_prompt, skip")
            continue

        try:
            candidates = ai_image.generate_image(
                prompt=prompt,
                n=n_candidates,
                size=image_size,
                output_dir=material_directory,
                provider_key=image_provider,
            )
        except ai_image.AIImageError as exc:
            logger.warning(
                f"ai_image: scene {getattr(scene, 'scene_id', '?')} generation failed: {exc}, skip"
            )
            continue
        if not candidates:
            continue

        if len(candidates) > 1 and select_best_caller is not None:
            chosen = consistency_filter.select_best_image(
                candidates=candidates,
                visual_desc=visual_desc,
                vlm_caller=select_best_caller,
            )
        else:
            chosen = candidates[0]

        image_paths.append(chosen.local_path)
        accumulated += target_duration
        if audio_duration > 0 and accumulated >= audio_duration:
            logger.info(
                f"ai_image: hit audio_duration target {audio_duration}s with {len(image_paths)} images, stop"
            )
            break

    logger.success(f"ai_image: produced {len(image_paths)} images")
    return image_paths


if __name__ == "__main__":
    download_videos(
        "test123", ["Money Exchange Medium"], audio_duration=100, source="pixabay"
    )
