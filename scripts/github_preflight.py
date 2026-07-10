import os
import re
import subprocess
import sys


MAX_BLOB_BYTES = int(os.environ.get("COLORCHASE_GITHUB_MAX_BLOB_BYTES", 100 * 1024 * 1024))
EXPECTED_BRANCH = os.environ.get("COLORCHASE_GITHUB_BRANCH", "codex/github-upload-clean")

BLOCKED_PATTERNS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)colorchase\.db"),
    re.compile(r"(^|/)生产环境密钥\.md$"),
    re.compile(r"(^|/)\.venv312(/|$)"),
    re.compile(r"(^|/)storage/"),
    re.compile(r"(^|/)uploads/"),
    re.compile(r"(^|/)uploaded/"),
    re.compile(r"(^|/)user_assets/"),
    re.compile(r"(^|/)user_configs/"),
    re.compile(r"(^|/)videos/"),
    re.compile(r"(^|/)model_assets/"),
    re.compile(r"(^|/)artifacts/models/"),
    re.compile(r"(^|/)weights/"),
    re.compile(r"(^|/)models/"),
    re.compile(r"(^|/)swinb_celeba_512/"),
    re.compile(r"(^|/)temp_luts/"),
    re.compile(r"(^|/)temp_frames/"),
    re.compile(r"(^|/)temp_neuralpreset/"),
    re.compile(r"(^|/)temp_train_data/"),
    re.compile(r"(^|/)training_corpus/"),
    re.compile(r"(^|/)\.trae/"),
    re.compile(r"(^|/)\.reasonix/"),
]


def run_git(args, input_text=None):
    result = subprocess.run(
        ["git", *args],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def fail(message):
    print(f"[FAIL] {message}")
    return 1


def main():
    errors = []

    branch = run_git(["branch", "--show-current"]).strip()
    if branch != EXPECTED_BRANCH:
        errors.append(f"当前分支是 {branch or '(detached)'}，建议切到 {EXPECTED_BRANCH} 后再推送")

    status = run_git(["status", "--short"]).strip()
    if status:
        errors.append("工作区不干净，请先提交或撤销未完成改动")

    tracked_files = [line.strip() for line in run_git(["ls-files"]).splitlines() if line.strip()]
    blocked_files = [
        path for path in tracked_files
        if any(pattern.search(path.replace("\\", "/")) for pattern in BLOCKED_PATTERNS)
    ]
    if blocked_files:
        errors.append("发现不应进入 GitHub 的跟踪文件:\n" + "\n".join(f"  - {path}" for path in blocked_files[:30]))

    objects = run_git(["rev-list", "--objects", "HEAD"])
    batch = run_git(["cat-file", "--batch-check=%(objecttype) %(objectname) %(objectsize) %(rest)"], objects)
    large_blobs = []
    for line in batch.splitlines():
        parts = line.split(" ", 3)
        if len(parts) < 4 or parts[0] != "blob":
            continue
        size = int(parts[2])
        path = parts[3]
        if size > MAX_BLOB_BYTES:
            large_blobs.append((size, path))
    if large_blobs:
        large_blobs.sort(reverse=True)
        lines = [f"  - {size / 1024 / 1024:.1f}MB {path}" for size, path in large_blobs[:30]]
        errors.append("发现超过限制的大文件:\n" + "\n".join(lines))

    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1

    max_file = max(
        (
            (int(line.split(" ", 3)[2]), line.split(" ", 3)[3])
            for line in batch.splitlines()
            if line.startswith("blob ") and len(line.split(" ", 3)) >= 4
        ),
        default=(0, ""),
    )
    print("[OK] GitHub 上传前检查通过")
    print(f"[OK] 当前分支: {branch}")
    print(f"[OK] 跟踪文件数: {len(tracked_files)}")
    print(f"[OK] 最大文件: {max_file[0] / 1024 / 1024:.2f}MB {max_file[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
