
import os
import sys
import time
import subprocess
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

def run_git(cmd):
    try:
        result = subprocess.run(
            cmd,
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            shell=True,
            encoding='utf-8',
            errors='replace',
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return False, "", str(e)

def get_modified_files():
    ok, stdout, stderr = run_git("git status --porcelain")
    if not ok:
        return []
    files = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        # line format: "M file.py" or "?? new.txt" etc.
        status = line[:2].strip()
        path = line[2:].strip()
        files.append((status, path))
    return files

def auto_commit_loop():
    repo = Path(__file__).parent
    print(f"🚀 自动提交监控启动 - {repo}")
    print("按 Ctrl+C 停止...")
    print()

    while True:
        files = get_modified_files()
        if files:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{now}] 检测到变更:")
            for st, f in files:
                print(f"  [{st}] {f}")

            # git add .
            run_git("git add .")

            # git commit
            msg = f"自动提交: {datetime.now().strftime('%Y%m%d_%H%M%S')}"
            ok, stdout, stderr = run_git(f'git commit -m "{msg}"')
            if ok:
                print(f"✅ 已提交: {msg}")
            else:
                print(f"⚠️ 提交结果: {stdout} {stderr}")
            print()

        time.sleep(5)  # 每5秒检查一次

if __name__ == "__main__":
    try:
        auto_commit_loop()
    except KeyboardInterrupt:
        print("\n👋 停止自动提交")

