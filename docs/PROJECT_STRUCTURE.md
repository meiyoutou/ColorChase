# ColorChase 项目目录与运行数据说明

> **重构后状态**：main.py 从 4624 行瘦身至 2996 行（减少 35.2%）
> **最后更新**：2026-06-29

## 1. 完整目录结构

```
ColorChase/
├── main.py                      # FastAPI 应用入口（2996 行，含核心追色主流程）
├── auth.py                      # JWT 鉴权与登录路由
├── config.py                    # 路径常量 + runtime user ContextVar
├── database.py                  # SQLAlchemy 异步 session
├── models.py                    # ORM 模型（Project 等）
├── progress.py                  # 全局 progress_manager 单例
├── admin_runtime_metrics.py     # ⚠️ 顶层模块（历史遗留，建议迁到 app/services/）
├── requirements.txt             # Python 依赖
├── .env.example                 # 环境变量样例
├── test_warm_cinema.xmp         # 测试用 XMP fixture
│
├── algorithms/                 # 算法层（不依赖 app/）
│   ├── __init__.py              # __getattr__ lazy import 路由
│   ├── color_transfer.py        # 经典颜色迁移算法
│   ├── color_continuity.py      # 视频色彩连续性
│   ├── depth_layers.py          # 景深分层（Depth Anything V2）
│   ├── postprocess.py           # 后处理（肤色保护、锐化等）
│   ├── segmentation.py          # 分割通用接口
│   ├── semantic_match.py       # 语义匹配（DINOv2）
│   ├── subject_mask.py          # 主体掩码（BiRefNet/SAM）
│   ├── dncm/                    # DNCM 神经网络训练与推理
│   │   ├── model.py
│   │   └── train.py
│   ├── metrics/                 # 图像质量指标
│   │   ├── content_similarity.py
│   │   ├── ldc.py
│   │   └── style_similarity.py
│   ├── modflows/                # Modflows 模型适配
│   │   └── adapter.py
│   ├── neural_preset/           # NeuralPreset 推理（有效版本）
│   │   └── inference.py
│   ├── neuralpreset/            # ⚠️ 重复目录（历史遗留，建议删除）
│   │   └── adapter.py
│   ├── segface/                 # 人脸分割
│   │   ├── inference.py
│   │   ├── model.py
│   │   └── transformer.py
│   └── video/                   # 视频处理
│       └── processor.py
│
├── app/                         # 应用层
│   ├── __init__.py
│   ├── security.py              # 上传大小校验、admin 开关、限流
│   ├── settings.py              # 环境变量解析
│   │
│   ├── routes/                  # FastAPI 路由模块（17 个）
│   │   ├── __init__.py
│   │   ├── admin.py             # 管理后台路由
│   │   ├── admin_models.py      # 模型管理 UI 路由
│   │   ├── analysis.py          # 景深/分割/语义分析路由（工厂函数）
│   │   ├── auth.py              # 登录/注册/token 路由
│   │   ├── files.py             # 静态文件服务路由（工厂函数）
│   │   ├── lut.py               # 🆕 LUT 合并 + Lightroom 预设路由
│   │   ├── meta.py              # 元数据路由（相机/算法清单）
│   │   ├── model_status.py      # 模型状态查询路由（工厂函数）
│   │   ├── portal.py            # 用户门户路由
│   │   ├── progress.py          # SSE 进度推送路由（工厂函数）
│   │   ├── projects.py           # 项目管理路由
│   │   ├── style_capture.py     # 风格捕获路由
│   │   ├── styles.py            # 🆕 风格列表/查询/重命名/应用路由
│   │   ├── task.py              # 任务控制 + user_config 路由（工厂函数）
│   │   ├── training.py          # 训练任务路由（工厂函数）
│   │   └── video_export.py      # 🆕 视频导出 + 元数据路由
│   │
│   └── services/                # 公共服务模块（6 个）
│       ├── __init__.py
│       ├── auth_utils.py        # 🆕 JWT 解码 + 用户身份提取
│       ├── model_management.py  # 🆕 模型管理状态 + 算法选择解析
│       ├── paths.py             # 🆕 项目路径解析 + 资产权限校验
│       ├── task_logging.py      # 任务日志写入器
│       └── training_corpus.py   # 训练语料 backfill
│
├── core/                        # 核心计算层（不依赖 app/routes/）
│   ├── __init__.py
│   ├── cache/
│   │   └── cache_manager.py     # 通用缓存
│   ├── color/
│   │   ├── bw_orange_filter.py   # 黑白橙滤镜
│   │   ├── cinematic_enhance.py  # 电影感增强
│   │   ├── color_refine.py       # 色彩精修
│   │   ├── colorspace.py         # 色彩空间转换
│   │   ├── lut_extractor.py      # LUT 提取
│   │   ├── lut_ops.py            # 🆕 LUT 生成 + 三线性插值（纯计算）
│   │   ├── skin_protect.py       # 肤色保护
│   │   └── tone_mapping.py       # 色调映射
│   ├── io/
│   │   ├── image_utils.py        # 🆕 cv2 图像读取 + base64 编码
│   │   ├── lut_parser.py         # LUT 文件解析
│   │   ├── lut_session.py        # 🆕 session LUT 加载 + style preset 落盘
│   │   ├── xmp_baker.py          # XMP 元数据写入
│   │   ├── export/
│   │   │   └── exporter.py       # 图片导出器
│   │   ├── loaders/
│   │   │   └── universal_loader.py  # 通用图像加载
│   │   └── metadata/
│   │       ├── exif_parser.py    # EXIF 解析
│   │       └── icc_parser.py      # ICC profile 解析
│   ├── render/
│   │   └── full_render.py        # 完整渲染管线（apply_lut 等）
│   └── style/
│       └── style_schema.py       # 风格 schema 定义
│
├── deploy/                      # 部署配置
│   ├── Caddyfile                # Caddy 反代配置
│   └── nginx-colorchase.conf    # Nginx 反代配置
│
├── docs/                        # 文档
│   ├── DEPLOYMENT_SECURITY.md
│   ├── GITHUB_UPLOAD.md
│   ├── PRODUCTION_REVERSE_PROXY.md
│   ├── PROJECT_STRUCTURE.md     # 本文档
│   └── RUNNING.md
│
├── scripts/                     # 运维脚本
│   ├── backfill_admin_task_logs.py  # 回填管理任务日志
│   └── github_preflight.py           # GitHub 上传预检
│
├── static/                      # 前端静态资源
│   ├── index.html
│   ├── admin-log-preview.html
│   ├── email-code-preview.html
│   ├── assets/
│   │   ├── logo.png
│   │   ├── icon-small.png
│   │   └── style-icon.png
│   ├── css/
│   │   └── style.css
│   └── js/
│       ├── app.js               # 主前端逻辑
│       ├── adjust_worker.js     # Web Worker
│       └── router.js            # 前端路由
│
├── tests/                       # 测试
│   ├── test_admin_training_corpus.py
│   ├── test_color_transfer.py
│   └── test_lut_extractor.py
│
└── storage/                     # 运行时数据（持久化，不入 git）
    └── (详见第 3 节)
```

## 2. 各模块职责一句话说明

### 顶层文件

| 文件 | 职责 |
|---|---|
| `main.py` | FastAPI 应用入口，含 `api_transfer`/`api_video_transfer`/`api_download_full`/`api_render_single` 等核心追色主流程 |
| `auth.py` | JWT 鉴权、登录/登出路由、SECRET_KEY/ALGORITHM 常量 |
| `config.py` | 路径常量（BASE_DIR/STORAGE_*）、runtime user ContextVar、`save_user_config` |
| `database.py` | SQLAlchemy 异步 session 工厂 |
| `models.py` | ORM 模型（Project 等） |
| `progress.py` | 全局 `progress_manager` 单例（任务进度状态） |
| `admin_runtime_metrics.py` | ⚠️ `record_model_call`/`record_user_usage`/`record_export`/`record_task_log` 等运行时指标记录 |

### app/services/（新建 + 已有）

| 模块 | 职责 |
|---|---|
| `paths.py` | 项目路径解析 + 资产权限校验（15 个函数，含 `_resolve_local_file_path`/`_safe_project_bucket_dir`/`_ensure_project_access` 等） |
| `auth_utils.py` | JWT 解码 + 用户身份提取（6 个函数，含 `_get_request_user_id`/`_get_request_user_role`/`_task_elapsed_ms`） |
| `model_management.py` | 模型管理状态查询 + 算法选择解析（9 个函数 + `MODEL_MANAGEMENT_PATH` 常量） |
| `task_logging.py` | `create_task_log_writer` 工厂，构造 `_write_task_log` |
| `training_corpus.py` | 训练语料 backfill 逻辑 |

### app/routes/（新建 3 个）

| 模块 | 职责 |
|---|---|
| `styles.py` | 风格列表/查询/重命名/应用（4 个路由） |
| `lut.py` | LUT 合并 + Lightroom DNG 预设打包（2 个路由 + `_create_minimal_dng` helper） |
| `video_export.py` | 视频导出转码 + 视频元数据读取（2 个路由） |

### core/color/ + core/io/（新建 3 个）

| 模块 | 职责 |
|---|---|
| `lut_ops.py` | LUT 纯计算（5 个函数：`apply_pro_adjust`/`_trilinear_lookup`/`_build_identity_lut`/`_generate_builtin_profile`/`_generate_orange_bw_lut`） |
| `lut_session.py` | LUT 文件 IO（2 个函数：`_load_lut_for_session`/`_save_lut_as_style_preset`） |
| `image_utils.py` | cv2 图像读取 + base64 编码 + 上传保存（4 个函数：`_save_upload`/`_cv2_imread_full`/`_cv2_imread`/`_img_to_base64`） |

## 3. 运行数据目录清单

```
storage/
├── cache/
│   ├── model_management.json    # 模型禁用/默认配置（运行时可改）
│   └── admin_metrics.json       # 管理指标快照
├── runtime/
│   └── <runtime_user_id>/       # 运行时用户隔离
│       ├── uploads/             # 临时上传
│       ├── videos/              # 视频源/导出
│       ├── temp_luts/           # session LUT 文件
│       │   └── <session_id>.npy
│       └── user_images/         # 用户图片
├── projects/
│   └── <project_id>/
│       ├── source/              # 项目源图
│       ├── reference/           # 参考图
│       ├── video_source/        # 项目视频源
│       ├── video_reference/     # 项目参考视频
│       ├── video_exports/      # 视频导出
│       ├── uploads/            # 项目上传
│       └── renders/            # 渲染结果
├── user_assets/
│   ├── images/                  # 用户公共图片
│   ├── references/              # 用户参考图
│   └── profiles/                # 用户配置
└── styles/
    └── extracted/               # 已提取的风格
        └── <style_id>/
            ├── lut_global.npy   # LUT 数据
            ├── style.ccs        # 风格元数据
            └── thumbnail.jpg    # 缩略图

presets/
└── orange_bw.npy                # 黑白橙 LUT 缓存（首次调用自动生成）
```

## 4. 部署持久化要求

### 4.1 必须持久化的目录

| 目录 | 说明 | 备份策略 |
|---|---|---|
| `storage/projects/` | 用户项目数据（源图/参考/导出） | **必须备份**，用户数据 |
| `storage/user_assets/` | 用户公共资产 | **必须备份** |
| `storage/styles/extracted/` | 已提取的风格 LUT | **必须备份**（含训练成果） |
| `storage/cache/model_management.json` | 模型管理配置 | 可备份（可重建） |
| `storage/runtime/<user_id>/temp_luts/` | session LUT | **无需备份**（临时数据） |
| `presets/orange_bw.npy` | 黑白橙 LUT 缓存 | **无需备份**（首次调用自动生成） |

### 4.2 环境变量（必须配置）

| 变量 | 说明 |
|---|---|
| `COLORCHASE_SECRET_KEY` | JWT 签名密钥（生产必改，禁止用默认值） |
| `COLORCHASE_ENABLE_PUBLIC_UPLOADS` | 是否允许公开上传 |
| `COLORCHASE_ENABLE_PUBLIC_VIDEOS` | 是否允许公开视频访问 |
| `COLORCHASE_ENABLE_LOCAL_ADMIN_TOOLS` | 是否启用本地 admin 工具（pick_folder 等） |
| `COLORCHASE_RUNTIME_USER_ID` | 运行时用户 ID（用于隔离非登录用户的临时数据） |

### 4.3 部署建议（Ubuntu 22.04 LTS）

- **Python 版本**：固定 3.10（不要升级到 3.12，sam2/depth-anything-v2 兼容性）
- **uvicorn 托管**：用 systemd `EnvironmentFile=` 加载 `.env`，别用 `nohup`
- **Nginx 反代**：`proxy_pass` + `buffering off`（SSE 流式响应需要）
- **`storage/` 单独挂盘**：用户上传 + 模型 cache 容易撑爆系统盘
- **GPU 驱动**（如用 CUDA）：`nvidia-driver-535` + `cuda-toolkit-12.1`

## 5. 已知历史遗留问题

### 5.1 `admin_runtime_metrics.py` 顶层模块

**问题**：`admin_runtime_metrics.py` 放在项目根目录，不在 `app/services/` 或 `core/` 中。

**影响**：[app/routes/styles.py](file:///D:/桌面/Trae临时文件/ColorChase/app/routes/styles.py)、[app/routes/lut.py](file:///D:/桌面/Trae临时文件/ColorChase/app/routes/lut.py)、[app/routes/video_export.py](file:///D:/桌面/Trae临时文件/ColorChase/app/routes/video_export.py) 都 `from admin_runtime_metrics import ...`，违反分层约定。

**建议**：迁移到 `app/services/admin_metrics.py`，全仓替换 import 路径。

### 5.2 `_user_profile_record` 跨路由私有函数导入

**问题**：[app/routes/projects.py](file:///D:/桌面/Trae临时文件/ColorChase/app/routes/projects.py) 的私有函数 `_user_profile_record`（下划线前缀）被以下模块跨路由导入：
- [main.py](file:///D:/桌面/Trae临时文件/ColorChase/main.py)（用于构造 `_write_task_log`）
- [app/routes/styles.py](file:///D:/桌面/Trae临时文件/ColorChase/app/routes/styles.py)
- [app/routes/lut.py](file:///D:/桌面/Trae临时文件/ColorChase/app/routes/lut.py)
- [app/routes/video_export.py](file:///D:/桌面/Trae临时文件/ColorChase/app/routes/video_export.py)

**影响**：违反"路由模块之间不应相互依赖"的原则。

**建议**：把 `_user_profile_record` 抽到 `app/services/user_profile.py`，所有模块从那里 import。

### 5.3 `algorithms/neuralpreset/` 重复目录

**问题**：`algorithms/neural_preset/`（有下划线）和 `algorithms/neuralpreset/`（无下划线）两个目录并存，头部代码相似（`SqueezeExcitation`、`MBConvBlock` 类定义相同）。

**影响**：代码重复，容易维护时改一处漏一处。

**建议**：先确认 `neuralpreset/` 是否被动态加载（grep `importlib` / 字符串拼接路径），如无引用则删除整个目录。

### 5.4 顶层散乱文件（已清理）

| 原文件 | 类型 | 当前位置 | 状态 |
|---|---|---|---|
| `_dl_depth.py` | 一次性下载脚本 | `scripts/dl_depth.py` | ✅ 已迁移 |
| `_download_sam.py` | 一次性下载脚本 | `scripts/download_sam.py` | ✅ 已迁移 |
| `auto_commit.py` | git 辅助工具 | `scripts/auto_commit.py` | ✅ 已迁移 |
| `test_warm_cinema.xmp` | 测试 fixture | `tests/fixtures/test_warm_cinema.xmp` | ✅ 已迁移 |
| `check_ckpt.py` | 调试脚本（硬编码 `D:\桌面\best.ckpt`） | — | ✅ 已删除 |

> 旧数据目录（`uploads/`、`uploaded/`、`user_assets/`、`training_corpus/`、`temp_luts/`）已全部迁移到 `storage/` 并删除根目录下的旧目录。

### 5.5 `_save_upload` 分层位置不纯

**问题**：`_save_upload` 依赖 `app.services.paths`（app 层），但被放在 `core/io/image_utils.py`（core 层）。严格来说 core 层不应依赖 app 层。

**影响**：架构纯洁性问题，不影响功能。

**建议**：把 `_save_upload` 单独迁到 `app/services/uploads.py`，让 `image_utils.py` 只保留 3 个纯 IO 函数。

## 6. 不建议动的区域（高风险块）

以下块仍在 main.py 中，**依赖的 helper 已全部抽离，但函数体本身未动**。改动风险高，建议业务稳定后再考虑：

### 6.1 `api_transfer`（图片追色主流程）

- **位置**：main.py 中段（约 1100 行）
- **风险点**：
  - 28 个 Form 参数，是前端最核心接口
  - 函数体内有 `trace_mark`/`trace_to_thread` 闭包，依赖外层 `task_id`/`algorithm`/`generate_lut_only`
  - 调用几乎所有 helper（`_resolve_transfer_model_runtime`/`_resolve_semantic_model_choice`/`_resolve_depth_model_choice`/`_resolve_mask_model_choice`/`_save_upload`/`_cv2_imread`/`apply_pro_adjust` 等）
  - 内嵌大量 lazy import（`algorithms.neural_preset`/`algorithms.modflows`/`algorithms.postprocess`）

### 6.2 `api_video_transfer` + `_background_video_transfer`

- **位置**：main.py 中段
- **风险点**：
  - 后台任务用 `asyncio.create_task` 启动
  - `_background_video_transfer` 内部 4 个闭包（`prog`/`mark_video_model_call`/`mark_video_task_success`/`mark_video_task_failure`）
  - 调用 `extract_frames`/`assemble_video`/逐帧追色循环

### 6.3 `api_download_full` + `api_render_single`

- **位置**：main.py 后段
- **风险点**：
  - 两个函数高度重复（都做 LUT 加载 + size_mode 分支 + apply_pro_adjust + 训练样本归档）
  - `download_full` 带 SSE 进度推送（`prog` 闭包），`render_single` 不带
  - 依赖 session 状态（`_apply_cached_depth_layers_if_any`/`_apply_cached_subject_mask_if_any`）

### 6.4 `lifespan` + `no_cache_static` 中间件

- **位置**：main.py 前段
- **风险点**：
  - `no_cache_static` 中间件做三件事（解析 runtime user + 设 ContextVar + 调 `begin_request_limits` 限流），顺序错会导致限流失效或用户身份丢失
  - `lifespan` 启动 `run_startup_legacy_asset_migration` + `run_startup_training_corpus_backfill` 两个后台任务

### 6.5 `_run_training_task`

- **位置**：main.py 中段
- **风险点**：
  - 调用 `algorithms.dncm.train_normalization_stage`/`train_stylization_stage`（torch 训练循环）
  - 内嵌 `send_stage_progress` 闭包通过 `asyncio.run_coroutine_threadsafe` 跨线程推送进度
  - 训练任务跑一次几十分钟，调试成本极高

---

## 附录：重构成果汇总

| 维度 | 数据 |
|---|---|
| main.py 原始行数 | 4624 行 |
| main.py 最终行数 | 2996 行 |
| 累计减少 | 1628 行（35.2%） |
| 完成的候选块 | 10 个 |
| 新建服务模块 | 3 个（paths、auth_utils、model_management） |
| 新建路由模块 | 3 个（styles、lut、video_export） |
| 新建核心模块 | 3 个（lut_ops、lut_session、image_utils 扩容） |
| 清理的重复代码 | 3 处（files.py 的 `_get_request_user_id`、styles/lut 的 `_cv2_imread`/`_img_to_base64`、styles/lut 的 `PREVIEW_MAX_SIZE`） |
| 依赖方向 | `routes/` → `services/` → `core/` → `config/`，无循环 |
