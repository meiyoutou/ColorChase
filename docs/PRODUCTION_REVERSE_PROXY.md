# ColorChase 生产反向代理配置

本文档用于部署 `https://colorchase.meiyoutou.top`。后端应用建议只监听本机：

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

公网只开放 80 和 443，通过 Nginx 或 Caddy 反向代理到 `127.0.0.1:8000`。

## 必填环境变量

服务器 `.env` 至少需要：

```env
COLORCHASE_ENV=production
COLORCHASE_SECRET_KEY=replace-with-your-real-long-secret
COLORCHASE_ALLOWED_ORIGINS=https://colorchase.meiyoutou.top,https://ColorChase.meiyoutou.top
COLORCHASE_ALLOWED_HOSTS=colorchase.meiyoutou.top,ColorChase.meiyoutou.top
COLORCHASE_UPLOAD_MAX_BYTES=10485760
COLORCHASE_IMAGE_ORIGINAL_UPLOAD_MAX_BYTES=314572800
COLORCHASE_VIDEO_UPLOAD_MAX_BYTES=314572800
COLORCHASE_UPLOAD_RATE_LIMIT=30
COLORCHASE_AI_RATE_LIMIT=12
COLORCHASE_GLOBAL_AI_CONCURRENCY=2
COLORCHASE_USER_AI_CONCURRENCY=1
```

不要把真实 `.env`、数据库、上传文件、生产密钥文档提交到 Git。

## Nginx

模板文件：

```text
deploy/nginx-colorchase.conf
```

使用前需要修改证书路径：

```nginx
ssl_certificate /etc/letsencrypt/live/colorchase.meiyoutou.top/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/colorchase.meiyoutou.top/privkey.pem;
```

推荐流程：

```bash
sudo cp deploy/nginx-colorchase.conf /etc/nginx/sites-available/colorchase.conf
sudo ln -s /etc/nginx/sites-available/colorchase.conf /etc/nginx/sites-enabled/colorchase.conf
sudo nginx -t
sudo systemctl reload nginx
```

Nginx 模板已包含：

- HTTP 自动跳转 HTTPS
- `client_max_body_size 300m`
- 上传接口基础限流
- API 基础限流
- SSE/长任务进度流所需的 `proxy_buffering off`
- 1 小时代理读写超时
- 常用安全响应头

## Caddy

模板文件：

```text
deploy/Caddyfile
```

Caddy 可以自动申请和续期 HTTPS 证书。模板只使用 Caddy 核心能力；频率限制由 ColorChase 应用自身处理。如果你后续安装了 Caddy 限流插件，可以再在代理层追加限流。

示例：

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 上线前验证

在服务器上验证：

```bash
python -m py_compile main.py app/settings.py app/security.py
python -c "import main; print('main import ok')"
```

浏览器访问：

```text
https://colorchase.meiyoutou.top
```

再确认：

- `/docs`、`/redoc`、`/openapi.json` 在生产环境不可访问
- 普通上传超过 10MB 会被拒绝
- 原图/视频上传最大 300MB
- 反向代理没有把后端 `127.0.0.1:8000` 暴露到公网
- GitHub 仓库里没有 `.env`、`colorchase.db`、`storage/`、`.venv312/`、上传目录和密钥文档
