# GitHub 上传说明

当前仓库有两个重要分支：

- `master`：本地完整开发历史，历史里曾经包含虚拟环境、模型权重和用户上传文件，不建议直接推到 GitHub。
- `codex/github-upload-clean`：干净上传分支，只包含当前源码快照和部署文档，适合推到 GitHub。

## 推送前检查

确认当前分支：

```bash
git branch --show-current
```

应该输出：

```text
codex/github-upload-clean
```

确认工作区干净：

```bash
git status
```

确认没有敏感文件：

```bash
git ls-files | grep -E '(^|/)(\.env$|colorchase\.db|生产环境密钥|\.venv312|storage/|uploads/|uploaded/|user_assets/|videos/|weights/|models/|swinb_celeba_512/)'
```

如果没有输出，说明这些文件没有进入当前分支。

## 首次上传到 GitHub

在 GitHub 创建一个空仓库，不要勾选自动创建 README、`.gitignore` 或 license。

添加远程仓库：

```bash
git remote add origin https://github.com/你的用户名/你的仓库名.git
```

把干净分支推到 GitHub 的 `main`：

```bash
git push -u origin codex/github-upload-clean:main
```

不要执行：

```bash
git push --all
git push origin master
```

这两个命令可能把本地旧历史推上去。

## 后续更新

建议继续在 `codex/github-upload-clean` 上做公开仓库更新，或者把这个分支作为 GitHub 的主分支。

每次推送前至少运行：

```bash
git status
git ls-files | grep -E '(^|/)(\.env$|colorchase\.db|生产环境密钥|\.venv312|storage/|uploads/|uploaded/|user_assets/|videos/|weights/|models/|swinb_celeba_512/)'
```

第二条命令没有输出，才继续推送。
