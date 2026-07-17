# 待执行迁移/脚本

| 脚本 | 用途 | 建议 |
|------|------|------|
| 202607_storage_label_dry_run.py | storage_label 目录迁移演练 | 执行 apply 前先跑一遍 |
| 202607_storage_label_apply.py | 真正迁移 training_corpus 目录 | 执行前必须完整备份 storage/ 和数据库 |

> 注意：`202607_storage_label_apply.py` 会真实移动文件，执行前务必做好备份。数据库备份已完成：`backups/colorchase_db_20260717_133551.sql`。
