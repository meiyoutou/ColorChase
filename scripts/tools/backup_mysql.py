#!/usr/bin/env python3
"""用 docker exec + mysqldump 备份 ColorChase MySQL 数据库。

读取项目根目录 .env 中的 COLORCHASE_DATABASE_URL。
备份文件保存在 storage/logs/backups/ 或 backups/ 目录。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "backups"
ENV_PATH = ROOT / ".env"


def _load_dotenv() -> None:
    """简单加载 .env，不依赖 python-dotenv。"""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _database_config() -> dict[str, str | int]:
    _load_dotenv()
    url = os.environ.get("COLORCHASE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("COLORCHASE_DATABASE_URL 未配置，无法连接 MySQL")
    parsed = urlparse(url)
    if not parsed.username or parsed.password is None:
        raise RuntimeError("数据库 URL 缺少用户名或密码")
    dbname = parsed.path.lstrip("/")
    if "?" in dbname:
        dbname = dbname.split("?", 1)[0]
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": dbname,
    }


def main() -> int:
    cfg = _database_config()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"colorchase_db_{timestamp}.sql"

    print(f"[backup] 目标文件: {backup_path}")
    print(f"[backup] 数据库: {cfg['dbname']} @ {cfg['host']}:{cfg['port']}")

    cmd = [
        "docker",
        "exec",
        "colorchase-mysql",
        "mysqldump",
        f"--host={cfg['host']}",
        f"--port={cfg['port']}",
        f"--user={cfg['user']}",
        f"--password={cfg['password']}",
        "--single-transaction",
        "--routines",
        "--triggers",
        str(cfg["dbname"]),
    ]
    masked = [arg if not arg.startswith("--password=") else "--password=***" for arg in cmd]
    print(f"[backup] 执行: {' '.join(masked)}")

    with backup_path.open("wb") as handle:
        proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.PIPE)

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="ignore") if proc.stderr else ""
        print(f"[backup] 失败，exit={proc.returncode}\n{stderr}", file=sys.stderr)
        if backup_path.exists():
            backup_path.unlink()
        return 1

    size = backup_path.stat().st_size
    print(f"[backup] 完成: {backup_path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
