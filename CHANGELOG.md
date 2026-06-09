# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
