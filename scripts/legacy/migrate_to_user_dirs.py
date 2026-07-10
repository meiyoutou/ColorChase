"""DEPRECATED / 已废弃。

此脚本仅保留历史参考；不适用于当前强制 MySQL 和 users.storage_label 方案。
不要在当前项目上运行。

历史背景：
- 旧脚本读取 SQLite colorchase.db。
- 旧脚本会移动 storage/projects/assets 下的真实目录。
- 当前项目已经切到强制 MySQL，并引入 users.storage_label 作为用户目录标识。

因此本文件保留为 legacy 入口，运行时会直接报错，避免误伤当前项目。
"""


def main():
    raise RuntimeError(
        "scripts/legacy/migrate_to_user_dirs.py 已废弃，仅保留历史参考；"
        "不适用于强制 MySQL 和 users.storage_label 方案，请不要运行。"
    )


if __name__ == "__main__":
    main()
