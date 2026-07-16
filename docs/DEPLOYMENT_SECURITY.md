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
