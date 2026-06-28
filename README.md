# ColorChase

ColorChase 是一个基于 FastAPI 的图像/视频追色工具，包含静态前端、用户项目管理、图片追色、视频追色、模型调用、训练样本管理和管理员统计能力。

## 目录说明

```text
api/                  业务 API 路由，包括认证、项目、管理员、模型管理和门户接口
algorithms/           图像追色、语义匹配、深度分层、主体 mask、后处理等算法
core/                 文件加载、缓存、渲染等底层通用能力
static/               前端静态页面、CSS、JS 和图标资源
styles/               内置风格和预设；运行时生成的 styles/extracted 不提交
presets/              项目内置预设
scripts/              维护、回填、模型下载等脚本
tests/                测试代码
docs/                 项目文档、部署安全清单和历史运行记录

main.py               FastAPI 应用入口，目前包含主要图片/视频/训练接口
auth.py               密码哈希、JWT 签发和生产密钥读取
config.py             运行路径和模型路径配置
database.py           数据库连接和初始化
models.py             SQLAlchemy 数据表模型
progress.py           长任务进度、暂停、恢复、取消管理
requirements.txt      Python 依赖
.env.example          生产环境变量模板，不包含真实密钥
```

## 不提交到 Git 的内容

以下内容是运行时数据或敏感配置，不应提交：

```text
.env
生产环境密钥.md
colorchase.db
uploaded/
uploads/
user_assets/
user_configs/
videos/
temp_luts/
temp_frames/
temp_neuralpreset/
temp_train_data/
training_corpus/
debug_output/
weights/
styles/extracted/
*.log
```

## 本地启动

安装依赖：

```bash
pip install -r requirements.txt
```

启动服务：

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

浏览器访问：

```text
http://127.0.0.1:8000
```

## 生产部署

生产部署前先阅读：

```text
docs/DEPLOYMENT_SECURITY.md
```

生产环境至少需要配置：

```env
COLORCHASE_ENV=production
COLORCHASE_SECRET_KEY=replace-with-a-long-random-secret
COLORCHASE_ALLOWED_ORIGINS=https://colorchase.meiyoutou.top,https://ColorChase.meiyoutou.top
```

建议后端只监听本机，由 Nginx 反向代理并启用 HTTPS。

## 上传和任务限制

默认限制：

```text
普通上传：10MB
图片追色原图：300MB
视频上传：300MB
上传请求：每用户/IP 每分钟 30 次
AI 请求：每用户/IP 每分钟 12 次
全站 AI 并发：2
单用户/IP AI 并发：1
```

这些限制可通过 `.env` 中的 `COLORCHASE_*` 环境变量调整，参考 `.env.example`。

## 提交前检查

每次提交前建议执行：

```bash
git status
git diff --stat
python -m py_compile auth.py api/auth.py main.py
```

确认 `.env`、密钥文档、数据库、用户上传文件和模型权重没有进入 Git。
