# ColorChase

基于 FastAPI 的图像/视频追色工具，集成 SAM2、Depth Anything V2、BiRefNet、DINOv2、DNCM、ModFlows 等深度模型，实现主体分割、景深分层、语义匹配与神经预设调色。

## 一、项目结构

```text
ColorChase/
├── main.py                     # FastAPI 入口，含追色主流程与高风险路由
├── auth.py                     # JWT 鉴权与登录
├── config.py                   # 路径常量、runtime user ContextVar、ensure_runtime_dirs
├── database.py                 # SQLAlchemy 异步 session
├── models.py                   # ORM 模型
├── progress.py                 # 全局 progress_manager（SSE 进度推送）
├── admin_runtime_metrics.py    # 运行时指标记录（历史遗留，待迁 app/services/）
├── requirements.txt
├── .env.example
│
├── app/                        # 应用层
│   ├── routes/                 # 路由模块（已模块化）
│   │   ├── auth.py             # 登录注册
│   │   ├── files.py            # 静态文件服务
│   │   ├── projects.py         # 项目管理
│   │   ├── styles.py           # 风格管理
│   │   ├── lut.py              # LUT 合并与预处理
│   │   ├── task.py             # 任务控制（暂停/恢复/取消）
│   │   ├── video_export.py     # 视频导出
│   │   ├── analysis.py         # 景深/语义/主体分割分析
│   │   ├── admin.py / admin_models.py  # 管理后台
│   │   ├── portal.py / meta.py / model_status.py / progress.py / style_capture.py / training.py
│   ├── services/               # 业务服务
│   │   ├── paths.py            # 路径与权限工具
│   │   ├── auth_utils.py       # JWT/身份工具
│   │   ├── model_management.py # 模型开关管理
│   │   ├── task_logging.py     # 任务日志写入
│   │   └── training_corpus.py # 训练语料管理
│   ├── security.py             # 上传大小/限速/本地工具开关
│   └── settings.py             # CORS/Host 白名单、环境判定、USER_SPACE_TZ
│
├── algorithms/                 # 算法层（不依赖 app/）
│   ├── color_transfer.py       # 经典颜色迁移
│   ├── depth_layers.py         # 景深分层（Depth Anything V2）
│   ├── semantic_match.py       # 语义匹配（DINOv2）
│   ├── subject_mask.py         # 主体分割（BiRefNet/SAM/MediaPipe）
│   ├── postprocess.py          # 后处理
│   ├── dncm/                   # DNCM 神经网络
│   ├── neural_preset/          # 神经预设推理（新）
│   ├── neuralpreset/           # 旧版适配（历史遗留，待清理）
│   ├── metrics/                # 风格/内容相似度
│   └── video/processor.py      # 视频处理
│
├── core/                       # 底层工具
│   ├── color/lut_ops.py        # LUT 纯计算
│   ├── io/image_utils.py       # 图像 IO（_save_upload 带扩展名白名单）
│   ├── io/lut_session.py       # LUT session 落盘
│   ├── io/loaders.py           # 图像加载器
│   └── render/full_render.py   # 全图渲染
│
├── deploy/                     # 部署配置
│   ├── nginx-colorchase.conf
│   └── Caddyfile
│
├── storage/                    # 运行时数据（不提交 Git）
│   ├── cache/                  # model_management.json、admin_runtime_metrics.json
│   ├── projects/               # 项目文件
│   ├── uploads/{images,videos}/ # 用户上传
│   ├── temp/{luts,frames}/     # 临时文件
│   ├── user_assets/             # 用户资源
│   ├── styles/extracted/        # 风格抽取结果
│   └── users/local_user/        # 用户画像
│
├── models/ 和 weights/         # 模型权重（不提交 Git）
├── presets/                    # 内置 LUT 预设（入库）
├── static/ templates/          # 前端资源
├── scripts/                    # 运维脚本（dl_depth、download_sam、auto_commit、github_preflight 等）
├── tests/                      # 测试（含 fixtures/）
└── docs/                       # 文档
```

## 二、运行方式

### 本地开发

```bash
# 1. 创建虚拟环境（Python 3.10+）
python -m venv venv
source venv/bin/activate    # Linux
venv\Scripts\activate       # Windows

# 2. 安装依赖
pip install -r requirements.txt
# sam2 与 depth-anything-v2 需从 GitHub 源码安装：
# pip install git+https://github.com/NielsRogge/sam2.git@v1.1.0
# pip install git+https://github.com/DepthAnything/Depth-Anything-V2.git

# 3. 准备 .env
cp .env.example .env
# 必填：COLORCHASE_SECRET_KEY、COLORCHASE_ENV=development

# 4. 启动
python main.py
# 默认监听 127.0.0.1:8033，访问 http://127.0.0.1:8033
```

### 生产部署（systemd + Nginx）

```bash
# 1. 克隆代码到 /opt/colorchase
git clone <repo> /opt/colorchase && cd /opt/colorchase

# 2. 安装依赖（建议用 conda 或 venv）
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. 配置 .env（生产必填项见下文"生产环境注意事项"）
cp .env.example .env && vim .env

# 4. 准备运行目录
mkdir -p storage/{cache,projects,uploads/{images,videos},videos,temp/{luts,frames},logs,users/local_user/{images,references,profiles},training/corpus,styles/extracted}
chown -R www-data:www-data storage/

# 5. 放置模型权重到 models/ 和 weights/（见下文）

# 6. 配置 systemd
cat > /etc/systemd/system/colorchase.service <<'EOF'
[Unit]
Description=ColorChase FastAPI
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/colorchase
EnvironmentFile=/opt/colorchase/.env
ExecStart=/opt/colorchase/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
systemctl enable --now colorchase

# 7. 配置 Nginx 反代
cp deploy/nginx-colorchase.conf /etc/nginx/sites-available/
ln -s /etc/nginx/sites-available/nginx-colorchase.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 8. 申请 HTTPS 证书
certbot certonly --webroot -w /var/www/certbot -d colorchase.meiyoutou.top -d ColorChase.meiyoutou.top
```

> 注意：`main.py` 末尾 `uvicorn.run` 默认端口需与 `deploy/nginx-colorchase.conf` 中 `server 127.0.0.1:XXXX;` upstream 一致，部署前务必核对。

## 三、上传目录与运行数据目录

| 目录 | 用途 | 清理策略 |
|---|---|---|
| `storage/uploads/images/` | 用户上传的原图 | 永久保留（用户资产） |
| `storage/uploads/videos/` | 用户上传的视频 | 永久保留 |
| `storage/projects/{id}/` | 项目文件（按项目隔离） | 跟随项目生命周期 |
| `storage/temp/luts/` | LUT 合并临时产物 | 任务结束后可清理 |
| `storage/temp/frames/` | 视频抽帧临时文件 | 建议 cron 清理 7 天以上 |
| `storage/cache/model_management.json` | 模型开关运行时配置 | 持久保留 |
| `storage/cache/admin_runtime_metrics.json` | 任务日志与统计 | 持久保留（含用户隐私） |
| `storage/styles/extracted/` | 风格抽取结果 | 跟随风格生命周期 |
| `storage/users/local_user/profiles/` | 用户头像与画像 | 永久保留 |

权限：`storage/` 整目录属主为运行用户（`www-data`），不通过 Nginx 直接静态分发。

## 四、模型与权重目录

项目使用两类模型权重，分别放在 `models/` 和 `weights/` 下：

| 模型 | 权重路径 | 用途 | 来源 |
|---|---|---|---|
| ModFlows B6 | `models/modflows/modflows_color_encoder_B6_dim_8195_iter_700000.pt` | 默认追色模型 | 项目内 |
| DNCM Neural Preset | `weights/neuralpreset/best.ckpt`、`norm_stage_best.pth` | 神经预设调色 | 项目内 |
| Style Stage | `weights/neural_preset/style_stage_best.pth` | 风格阶段（**部署前确认存在**） | 项目内 |
| SAM2 | `weights/sam2/sam2_hiera_base_plus.pt`、`sam2_hiera_small.pt` | 主体分割 | GitHub Release |
| Depth Anything V2 | `weights/depth_anything_v2/vitl.pth`、`vitb.pth` | 景深分层 | HuggingFace |
| BiRefNet | HF Hub `ZhengPeng7/BiRefNet` | 主体分割 | `transformers` 自动下载 |
| DINOv2 | HF Hub `facebook/dinov2-small` | 语义匹配 | `transformers` 自动下载 |

模型开关通过 `storage/cache/model_management.json` 控制 `disabled_models` 与 `default_model`，可在管理后台 `/api/admin/models` 动态调整。

部署 BiRefNet/DINOv2 时若服务器无法直连 HuggingFace，需预下载到 `~/.cache/huggingface/hub/` 或设置 `HF_ENDPOINT=https://hf-mirror.com`。

## 五、部署方式

- **推荐系统**：Ubuntu 22.04 LTS（Python 3.10 与 sam2/depth-anything-v2/mediapipe 兼容性最佳）
- **反代**：Nginx（`deploy/nginx-colorchase.conf`）或 Caddy（`deploy/Caddyfile`，自动 HTTPS）
- **进程管理**：systemd（推荐）或 supervisor
- **SSE 必须关 buffering**：Nginx 已配 `proxy_buffering off`，Caddy 已配 `flush_interval -1`，否则进度条不刷新
- **上传限制**：Nginx `client_max_body_size 300m`，代码侧 `COLORCHASE_VIDEO_UPLOAD_MAX_BYTES=314572800`，三者必须一致

## 六、生产环境注意事项

部署前必须完成以下检查（详见 `docs/` 中部署检查清单）：

1. `.env` 设置 `COLORCHASE_ENV=production`（否则 `/docs`、`/redoc` 暴露，CORS 走 `*`）
2. `.env` 设置强随机 `COLORCHASE_SECRET_KEY`（默认值公开在源码，JWT 可被伪造）
3. `storage/` 目录创建并赋权（`ensure_runtime_dirs` 在启动时创建 13 个子目录，父目录无写权限会启动失败）
4. 模型权重齐备（特别是 `style_stage_best.pth`，缺失会导致 neural preset 推理抛 FileNotFoundError）
5. BiRefNet/DINOv2 的 HF 缓存预下载或镜像配置
6. `requirements.txt` 锁版本（当前除 sam2/depth-anything-v2 外均未锁版本，`pip install` 可能拉到不兼容版本）
7. HTTPS 证书签发（Nginx 模板已预填 Let's Encrypt 路径）
8. `_resolve_local_file_path` 已加固（拒绝绝对路径与 `..`，限定 BASE_DIR 下），生产前复核调用点
9. `algorithms/neuralpreset/adapter.py` 含硬编码 Windows 路径 `D:\桌面\best.ckpt`，Linux 部署前必须清理
10. `_save_upload` 已加扩展名白名单（`.jpg/.jpeg/.png/.webp/.gif/.bmp/.tiff/.mp4/.mov/.avi`），如需扩展格式同步更新 `core/io/image_utils.py`

## 七、开发时不提交 Git 的目录

`.gitignore` 已覆盖以下内容，**严禁** `git add` 强制提交：

| 类型 | 路径 | 原因 |
|---|---|---|
| 运行时数据 | `storage/`、`uploads/`、`user_assets/`、`user_configs/`、`videos/`、`training_corpus/`、`temp_*` | 含用户上传文件与隐私 |
| 模型权重 | `model_assets/`、`models/`、`weights/`、`*.pt`、`*.pth`、`*.ckpt`、`*.onnx` | GB 级大文件，污染仓库历史 |
| 密钥配置 | `.env`、`user_config.json`、`生产环境密钥.md` | 含 JWT secret、SMTP 密码 |
| 数据库 | `colorchase.db`、`*.sqlite`、`*.sqlite3` | 运行时数据 |
| IDE | `.vscode/`、`.idea/`、`.trae/`、`.reasonmix/` | 本地配置 |

推送前可运行 `python scripts/github_preflight.py` 检查大文件与敏感文件（默认分支 `codex/github-upload-clean`）。
