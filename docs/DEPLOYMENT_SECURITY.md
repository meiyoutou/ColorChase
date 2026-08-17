# ColorChase 生产部署安全清单

本文档用于部署 `https://colorchase.meiyoutou.top` 前的安全检查。不要把真实 `.env`、数据库、上传文件、模型权重或密钥文档提交到 Git。

## 1. Git 提交前检查

每次提交前执行：

```bash
git status
git diff --stat
git diff
```

确认以下文件没有进入提交：

- `.env`
- `生产环境密钥.md`
- `colorchase.db`
- `uploaded/`
- `uploads/`
- `user_assets/`
- `user_configs/`
- `temp_train_data/`
- `temp_luts/`
- `videos/`
- `weights/`
- `styles/extracted/`
- `*.log`

如果这些文件曾经推送到公开远程仓库，需要清理 Git 历史，并更换相关密钥。

## 2. 生产环境变量

服务器上使用 `.env.example` 作为模板创建 `.env`。必须设置：

```env
COLORCHASE_ENV=production
COLORCHASE_SECRET_KEY=replace-with-a-long-random-secret
# 可选：追加默认白名单以外的前端 origin。默认已包含生产站和 GitHub Pages。
COLORCHASE_ALLOWED_ORIGINS=
```

生成密钥：

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

还需要配置 SMTP：

```env
CC_SMTP_HOST=smtp.qq.com
CC_SMTP_USER=your-email@example.com
CC_SMTP_PASS=your-smtp-authorization-code
```

生产环境缺少 `COLORCHASE_SECRET_KEY` 时，服务会拒绝启动。

## 3. 上传和任务限制

默认限制：

```env
COLORCHASE_UPLOAD_MAX_BYTES=10485760
COLORCHASE_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES=314572800
COLORCHASE_VIDEO_UPLOAD_MAX_BYTES=314572800
COLORCHASE_UPLOAD_RATE_LIMIT=30
COLORCHASE_AI_RATE_LIMIT=12
COLORCHASE_GLOBAL_AI_CONCURRENCY=2
COLORCHASE_USER_AI_CONCURRENCY=1
```

含义：

- 普通上传：10MB
- 图片追色原图：300MB
- 视频上传：300MB
- 上传请求：每用户/IP 每分钟 30 次
- AI 请求：每用户/IP 每分钟 12 次
- 全站 AI 并发：2
- 单用户/IP AI 并发：1

## 4. 服务器网络

公网只开放：

- 80
- 443

后端应用不要直接暴露公网，建议只监听本机：

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

使用 Nginx 反向代理到 `127.0.0.1:8000`。

## 5. HTTPS

必须启用 HTTPS。建议使用 Nginx + Let's Encrypt 证书，并将 HTTP 自动跳转 HTTPS。

正式访问地址：

```text
https://colorchase.meiyoutou.top
```

## 6. SSH 和系统安全

建议：

- 使用 SSH key 登录
- 禁止 root 密码登录
- 关闭 SSH 密码登录
- 安装 fail2ban 或启用云厂商登录防护
- 定期更新系统补丁

## 7. 数据和备份

生产环境不要使用本机开发数据库。上线前创建干净数据库和独立数据目录。

至少备份：

- 生产数据库
- 用户上传目录
- 服务器 `.env`
- `COLORCHASE_SECRET_KEY`

## 8. 上线前验证

在服务器上执行：

```bash
python -m py_compile auth.py api/auth.py main.py
```

确认：

- 无 `COLORCHASE_SECRET_KEY` 时生产环境拒绝启动
- 有 `COLORCHASE_SECRET_KEY` 时服务正常启动
- 登录、注册、验证码发送正常
- 普通上传超过 10MB 会被拒绝
- 图片追色原图 300MB 内可上传
- 用户不能访问其他用户项目资源

## 9. GPU 与 PyTorch 版本选择

AI 人像追色依赖 NeuralPreset（EfficientNet-B0）和 SegFace（Swin-B）两个模型。CPU 上跑约 290 秒，GPU 上 10-30 秒。**有 GPU 必须装 CUDA 版 PyTorch，否则追色慢到没法用。**

### 9.1 检查 GPU 硬件和驱动

```bash
nvidia-smi
```

看两个值：
- `GPU Name`：有 NVIDIA 显卡才能用 CUDA（无输出或只有核显 → 走 9.5 的 CPU 方案）
- `CUDA Version`：驱动支持的最大 CUDA 版本（如 12.0、12.6）

### 9.2 根据驱动 CUDA 版本选 PyTorch 渠道

| 驱动支持 CUDA 版本 | PyTorch 渠道 | 可用 torch 版本 | 安装命令 |
|---|---|---|---|
| >= 12.6 | cu126 | 2.12.0+cu126（最新，零降级） | `pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126` |
| >= 12.4 | cu124 | 最高 2.6.0+cu124 | `pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124` |
| >= 12.1 | cu121 | 最高 2.5.1+cu121 | `pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121` |
| 无 GPU | CPU（PyPI 默认） | 2.12.0+cpu | `pip install torch torchvision` |

注意：
- cu126 自带 CUDA 12.6 runtime 库，不依赖系统装 CUDA Toolkit，但**显卡驱动必须足够新**
- 驱动太旧（如 526.56 / CUDA 12.0）想用 cu126，先去 NVIDIA 官网或 GeForce Experience 升级驱动
- 不要混装 CPU 版和 CUDA 版：先 `pip uninstall torch torchvision -y` 再装 CUDA 版，否则 `torch.cuda.is_available()` 可能返回 False

### 9.3 安装后验证

```bash
python -c "import torch; print(torch.__version__, 'cuda:', torch.cuda.is_available(), 'devices:', torch.cuda.device_count())"
```

有 GPU 时期望输出：
```
2.12.0+cu126 cuda: True devices: 1
```

如果 `cuda: False`，依次检查：
1. 版本号是否带 `+cpu`（带说明装成了 CPU 版，重装）
2. `nvidia-smi` 的驱动版本是否支持对应 CUDA
3. 是否有残留 CPU 版 torch 没卸干净（`pip show torch` 看安装来源）

### 9.4 依赖兼容性

装 CUDA 版 torch 后，确认这些包仍兼容（`requirements.txt` 里依赖 torch 的）：

| 包名 | torch 版本要求 | 备注 |
|---|---|---|
| `transformers` | `>=2.4` | cu126 的 2.12.0 满足 |
| `sam2==1.1.0` | 无上限 | 兼容 |
| `depth-anything-v2==0.1.0` | 无上限 | 兼容 |

降级 torch（如用 cu121 的 2.5.1）时，`transformers` 要求 `>=2.4` 仍满足，但建议优先用 cu126 零降级方案。

### 9.5 无 GPU 服务器的处理

CPU 版 PyTorch 能跑，但 AI 人像追色单张约 290 秒（近 5 分钟），生产环境不推荐。可选：
- 升级服务器到带 GPU 的实例
- AI 追色任务转发到有 GPU 的推理服务
- 降低模型精度或减少后处理步骤（牺牲质量换速度）
