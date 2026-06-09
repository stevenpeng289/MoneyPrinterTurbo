# 跨境物流行业主题素材包

> 改造 D-3：内置行业主题包，让 Steven 开箱即用。

## 用途

为口播/科普/案例类短视频快速提供"行业感"素材，避免每次都从 Pexels 现搜。

## 包含内容（待用户运行 `download_pack.py` 填充）

预设 50 个跨境物流相关搜索词（搜 Pexels）：
- 仓储：warehouse / forklift / cargo box / pallet / shelving
- 物流：cargo ship / container / truck / freight / logistics worker
- 跨境：customs / international shipping / global trade / port crane
- 电商场景：online shopping / delivery package / parcel scanning
- 数据：barcode scanner / inventory tracking / shipping label

完整搜索词清单见 `metadata.yaml.search_terms`。

## 使用步骤

```bash
# 1. 准备 Pexels API Key（免费，注册 https://www.pexels.com/api/）
export PEXELS_API_KEY="your_key_here"

# 2. 运行下载脚本（默认每个搜索词下载 1 个视频，共约 50 个）
uv run python resource/material_packs/cross_border_logistics/download_pack.py

# 3. 自动打标（用 multimodal LLM 给每个视频生成 yaml）
# 启动 FastAPI 服务后调用：
curl -X POST http://127.0.0.1:8080/api/v1/material-scan \
  -H 'Content-Type: application/json' \
  -d '{"base_dir": "resource/material_packs/cross_border_logistics"}'

# 4. 在 config.toml 把 material_directory 指向这里
material_directory = "resource/material_packs/cross_border_logistics"
```

## 自定义主题包

任何包含 `metadata.yaml` 的目录都可以作为主题包，结构：

```
resource/material_packs/your_topic/
├── README.md
├── metadata.yaml      # 必需，描述主题、搜索词、推荐时长等
├── download_pack.py   # 可选，自动从 Pexels 拉素材
├── video1.mp4
├── video1.yaml        # auto_tagger 自动生成
└── ...
```

## 主题包列表查询

```bash
curl http://127.0.0.1:8080/api/v1/material-packs
```
