# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **改造 D：本地素材库智能化（AI 自动打标 + 主题包 + 增量扫描）** （feat/mpt-enhancement-2026q2）
  - `app/services/auto_tagger.py`：单视频自动打标
    - ffmpeg 抽 N 帧（开头/中间/结尾均匀分布，跳过 0% 和 100% 边界）
    - LLM 调用通过 `TagCaller` 注入（与 RAG 同样的设计模式）
    - `parse_tags()`：剥 markdown 围栏 / bracket fallback / lowercase 归一化 / dedup / null 跳过
    - **人工保护**：`<video>.yaml.locked` 文件存在时直接跳过覆盖
    - 已有 yaml 默认不覆盖（`overwrite=True` 才重打）
    - 帧文件用临时目录，函数返回自动清理
  - `app/services/material_scanner.py`：批量增量扫描
    - cache 文件 `.material_scan_cache.json`，按 mtime 增量
    - `max_videos` 限流，首次全量扫描可分批跑
    - 路径白名单走 `file_security.resolve_path_within_directory`
    - `MaterialScanReport` 五项指标：scanned / tagged / skipped_locked / skipped_unchanged / failed
  - `app/services/llm_multimodal.py`：默认多模态 caller
    - OpenAI 兼容 vision API（MiniMax / OpenAI / 月之暗面等都通用）
    - 帧文件 base64 inline 到 message，零外网托管依赖
    - 降级模式：API key 不全时自动切到 filename-only fallback
  - `app/controllers/v1/material.py`：素材库 controller
    - `GET /api/v1/material-packs`：列出 `resource/material_packs/` 下的主题包
    - `POST /api/v1/material-scan`：触发自动打标扫描
  - `app/router.py`：注册 `material.router`
  - `app/models/schema.py`：`MaterialPackInfo` / `MaterialPackListResponse` / `MaterialScanRequest` / `MaterialScanResponse`
  - `resource/material_packs/cross_border_logistics/`：内置跨境物流主题包
    - `metadata.yaml`：27 个 Pexels 搜索词（仓储 / 物流 / 跨境 / 电商 / 数据工具）
    - `download_pack.py`：批量从 Pexels 拉素材脚本（用户填 API key）
    - `README.md`：使用说明
  - `config.example.toml`：`[app]` 加 5 个 auto_tagger 配置项
    - `auto_tagger_enabled` 默认 false（手动启用，避免误调 LLM 烧 token）
    - `auto_tagger_num_frames` / `auto_tagger_provider` / `auto_tagger_model_label` / `auto_tagger_max_videos_per_scan`
  - `test/services/test_material_scanner.py`：26 个单元测试
    - parse_tags 容错（含 null/dedup/lowercase）
    - timestamp 计算
    - yaml 读写兼容 `search_videos_local`
    - tag_video lock 文件保护 + 已有 yaml 不覆盖
    - 增量缓存 / overwrite 触发重打标 / max_videos 限流
    - controller 端到端（启停 / 路径越界 / 成功扫描）

- **改造 A：RAG 长脚本拆分** （feat/mpt-enhancement-2026q2）
  - `app/services/prompts/long_storyboard.py`：长脚本拆分 prompt 模板，严格 JSON 输出约束
    - `LONG_STORYBOARD_SYSTEM_PROMPT`：角色 + 输出规则 + 切分规则 + 质量门
    - `build_long_storyboard_prompt()`：动态拼接原文 + 检索上下文
  - `app/services/rag_storyboard.py`：RAG pipeline 主逻辑
    - `split_long_text()`：用 `langchain-text-splitters.RecursiveCharacterTextSplitter`，中文友好分隔符
    - `retrieve_top_k_chunks()`：v1 实现用 n-gram overlap 排序（不引入 chromadb，保持轻量）
    - `parse_episodes()`：JSON 解析 + 自动剥 markdown 围栏 + bracket fallback + keywords 字符串兼容 + duration clamp [30, 90]
    - `generate_long_storyboard()`：主入口，注入式 `llm_caller`（便于测试 mock + 避免循环导入）
    - 完整错误体系：`LongStoryboardError` / `InputTooShortError` / `InputTooLongError` / `LLMOutputParseError` / `EpisodeValidationError`
    - `@dataclass(frozen=True) EpisodeDraft`：不可变结果对象
    - 失败自动重试（默认 3 次）
  - `app/services/llm.py`：`generate_long_storyboard()` 入口
    - 复用现有 `_generate_response()` 通道，零侵入兼容所有 provider
  - `app/models/schema.py`：`LongStoryboardRequest` / `EpisodeDetail` / `LongStoryboardResponse`
    - 输入校验：`text` ∈ [200, 200_000] 字，`chunk_size` ∈ [500, 8000]
  - `app/controllers/v1/llm.py`：`POST /api/v1/long-storyboard`
    - 业务错误分流：太短/太长 400，LLM 多次失败 502
  - `test/services/test_rag_storyboard.py`：33 个单元测试
    - 切块边界、检索排序、JSON 解析容错、duration clamp、retry 机制、prompt 构造、TestClient 端到端

- **改造 B：行业脚本模板库** （feat/mpt-enhancement-2026q2）
  - `app/services/prompts/templates/`：5 个跨境物流/电商行业脚本模板
    - `cross_border_policy`（政策解读，三段式）
    - `cloud_warehouse_promo`（云仓种草，痛点→方案→召唤）
    - `industry_insider`（行业内幕，钩子+证据+启示+收获）
    - `customer_case`（客户案例，背景→痛点→改变→结果）
    - `product_compare`（对比，维度+条件式结论）
  - `ScriptTemplate` 用 `@dataclass(frozen=True)` + `__post_init__` 字段强校验
  - `TEMPLATE_REGISTRY` 用 `MappingProxyType` 防外部篡改
  - `app/services/llm.py`：`list_templates()` / `load_template()` / `generate_script_from_template()` / `UnknownTemplateError`
    - 复用现有 `generate_script()` 通道，模板只覆盖 system prompt + few-shot
  - `app/models/schema.py`：`TemplateBrief` / `TemplateDetail` / `TemplateListResponse` / `TemplateDetailResponse`
  - `VideoParams.template_id`、`VideoScriptParams.template_id` 字段
  - `app/controllers/v1/llm.py`：`GET /api/v1/templates`、`GET /api/v1/templates/{id}`；`POST /api/v1/scripts` 支持 `template_id` 分支
  - `webui/Main.py`：📋 套用行业模板下拉框 + 模板预览 + 与 custom_system_prompt 互斥
  - `webui/i18n/{zh,en,ru,de,vi,tr,pt}.json`：3 个新 i18n key
  - `test/services/test_templates.py`：25 个单元测试（注册表 / dataclass 校验 / LLM 接口 / schema / TestClient 端到端）
  - 依赖：`langchain-text-splitters`（A 用预装）、`ffmpeg-python`（D 用预装）、`pytest`+`pytest-asyncio`（dev）

- **本地视频素材搜索** (`app/services/material.py:search_videos_local`)
  - 扫描 `material_directory` 下的本地 `.mp4` 文件
  - 读同名 `.yaml` 描述文件的 `tags` 列表
  - `search_term` ↔ `tags` 双向子串匹配（大小写无关）
  - 用 `file://` 协议返回，`save_video` 短路处理（不下载、不改名）
  - 加 `local_search` 视频源分支到 `download_videos()`

- **config.toml 环境变量占位符** (`app/config/config.py:_expand_env`)
  - 支持 `${VAR}` 占位符递归替换为 `os.environ` 值
  - 字符串、列表、字典三种类型都支持
  - 找不到环境变量时保持原样（不报错）
  - 适用场景：Docker / K8s 部署时通过环境变量注入密钥

- **`output_dir()` 工具函数** (`app/utils/utils.py`)
  - 返回项目根目录下 `output/` 路径
  - 与现有 `storage_dir()` 同模式（支持 `sub_dir` + `create` 参数）

- **视频产物持久化到 `output/`** (`app/services/task.py:generate_final_videos`)
  - 每个视频生成完成后 `shutil.copy2` 到 `output/<subject>_<timestamp>.mp4`
  - 文件名清洗非法字符 `\/:*?"<>|` → `_`
  - `video_count > 1` 时加 `_<index>` 后缀
  - 时间戳格式：`YYYYMMDD_HHMMSS`

### Changed

- **`text_background_color` 默认值统一为 `"#00000080"`** (含 alpha 通道的半透明黑)
  - `app/models/schema.py`：`VideoParams.text_background_color` 默认 `True` → `"#00000080"`
  - `app/models/schema.py`：`SubtitleRequest.text_background_color` 默认 `True` → `"#00000080"`
  - `app/services/video.py:generate_video`：布尔值兼容路径同步从 `"#000000"` 改为 `"#00000080"`
  - 视觉效果：默认开启半透明黑色字幕底板（替代原本的纯黑底板）

### Notes

- 上述改动是本地 fork 增量，**未合入 upstream**。upstream MPT v1.2.9 不包含 `search_videos_local()` 等本地功能。
- 配套的 `assets/`、`create_samples.sh`、`output/` 为个人使用文件，**未纳入版本控制**（已加入 `.gitignore`）。
- 下一阶段计划在 `search_videos_local()` 基础上加 LLM 自动打标（改造 D），见 `~/.claude/plans/shimmering-cooking-mango.md`。

[Unreleased]: https://github.com/harry0703/MoneyPrinterTurbo/compare/v1.2.9...HEAD
