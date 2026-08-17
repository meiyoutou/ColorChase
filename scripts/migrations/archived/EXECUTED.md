# 已执行迁移记录

| 脚本 | 执行时间 | 执行环境 | 操作人 | 结果 | 备注 |
|------|----------|----------|--------|------|------|
| 202607_add_users_storage_label.py | 2026-07-16 | 本地开发 | - | 成功 | 添加 users.storage_label 字段 |
| 202607_promote_super_admin.py | 2026-07-16 | 本地开发 | - | 成功 | 将账号 11 提升为 super_admin |
| 202607_create_admin_meiyoutou.py | 2026-07-17 | 本地开发 | - | 成功 | 创建管理员 meiyoutou |
| backfill_admin_task_logs.py | 2026-07-17 | 本地开发 | - | 成功 | 回填管理员任务日志角色信息，共 39 条日志，更新 24 条 |
| 202607_storage_label_apply.py | 2026-07-17 | 本地开发 | - | 成功 | 迁移 training_corpus 到 storage_label 目录；本次 source_missing=18，moved=0（此前已迁移） |
