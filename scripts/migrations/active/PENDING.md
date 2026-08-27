# 待执行迁移/脚本

| 脚本 | 用途 | 建议 |
|------|------|------|
| 202607_storage_label_dry_run.py | storage_label 目录迁移演练 | 如需重新评估目录结构时运行 |
| 202608_storage_quota_schema.py | 检查/补齐配额表在中间开发版本后新增的状态字段 | 部署本功能代码前先 dry-run；确认专用数据库备份后再 `--apply`。该动作不等于启用配额 |

> 注意：`202607_storage_label_apply.py` 已执行并归档。空 legacy 目录 `storage/training/corpus/11` 和 `storage/training/corpus/1375320002@qq.com` 已清理。