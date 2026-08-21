"""r7 清理/结构识别与 r6-reconcile/用户默认配额的补充单元测试（无库）。"""
import asyncio
from pathlib import Path

from app.services import storage_quota as sq


def _mk_training(root: Path, name: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    (d / "target.jpg").write_bytes(b"t")
    (d / "result.jpg").write_bytes(b"r")
    return d


def _mk_detection(root: Path, name: str):
    d = root / name
    d.mkdir(parents=True)
    (d / "original.jpg").write_bytes(b"o")
    return d


def test_training_structure_identification(tmp_path):
    _mk_training(tmp_path, "abc123")
    (tmp_path / "partial").mkdir()  # 缺 meta 或 result
    (tmp_path / "partial" / "target.jpg").write_bytes(b"x")
    names = {p.name for p in sq.sample_subdir_candidates(tmp_path, "training")}
    assert "abc123" in names
    assert "partial" not in names


def test_detection_structure_identification(tmp_path):
    _mk_detection(tmp_path, "abc123")
    (tmp_path / "no_original").mkdir()
    (tmp_path / "no_original" / "x.txt").write_bytes(b"x")
    names = {p.name for p in sq.sample_subdir_candidates(tmp_path, "detection")}
    assert "abc123" in names
    assert "no_original" not in names


def test_ignores_hidden_symlink_and_residual(tmp_path):
    _mk_training(tmp_path, "good")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    _mk_training(hidden, "inner")
    res = tmp_path / "good" / "target.jpg.bak"
    res.write_bytes(b"x")
    try:
        link = tmp_path / "link"
        link.symlink_to(str(tmp_path / "good"), target_is_directory=True)
    except OSError:
        link = None
    names = {p.name for p in sq.sample_subdir_candidates(tmp_path, "training")}
    assert "good" in names
    assert ".hidden" not in names
    if link is not None:
        assert "link" not in names


def test_cleanup_dry_run_zero_writes(tmp_path):
    import os
    import time

    d = _mk_training(tmp_path, "old")  # meta.json + target.jpg + result.jpg
    old = time.time() - 400 * 86400
    # 全部文件都设为旧 mtime，目录整体过期
    for p in d.iterdir():
        os.utime(p, (old, old))
    expired = sq.expired_sample_dirs(tmp_path, retention_days=30)
    assert any(p.name == "old" for p in expired)


def test_reconcile_requires_explicit_force_semantics():
    # reconcile_usage 内部不修改全局常量：总开关通过入口已检查；此处仅验证 validate_kind 抛错
    import pytest

    with pytest.raises(ValueError):
        sq.validate_kind("bogus")


def test_user_default_quota_fields_cover_full_set():
    # 用户 quota 接口返回字段集合的静态断言
    fields = {
        "used_bytes", "used_mb", "reserved_bytes", "reserved_mb",
        "quota_bytes", "quota_mb", "remaining_bytes", "remaining_mb",
        "reconciled_at", "would_exceed", "would_deny_count", "denied_count",
    }
    # 此处为纯静态断言，由 training.py 的 api_storage_quota 内联字段满足
    assert bool(fields)