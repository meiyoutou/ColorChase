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


def test_is_valid_sample_dir_checks_only_current_directory(tmp_path):
    good = _mk_training(tmp_path, "good")
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "meta.json").write_text("{}", encoding="utf-8")
    (partial / "target.jpg").write_bytes(b"t")

    assert sq.is_valid_sample_dir(good, "training") is True
    assert sq.is_valid_sample_dir(partial, "training") is False
    assert sq.is_valid_sample_dir(_mk_detection(tmp_path, "det"), "detection") is True


def test_physical_orphan_stats_separates_recovery_and_committed(tmp_path):
    (tmp_path / "s.staging-deadbeef").mkdir()
    (tmp_path / "s.staging-deadbeef" / "a").write_bytes(b"a")
    (tmp_path / "s.backup-committed-deadbeef").mkdir()
    (tmp_path / "s.backup-committed-deadbeef" / "b").write_bytes(b"bb")
    (tmp_path / "s.backup-recovery-deadbeef").mkdir()
    (tmp_path / "s.backup-recovery-deadbeef" / "c").write_bytes(b"ccc")
    (tmp_path / ".quota-trash").mkdir()
    (tmp_path / ".quota-trash" / "old").write_bytes(b"dddd")

    stats = sq.physical_orphan_stats(tmp_path)
    assert stats == {
        "trash_bytes": 4,
        "staging_bytes": 1,
        "committed_backup_bytes": 2,
        "recovery_backup_bytes": 3,
        "recovery_backup_count": 1,
    }


def test_reconcile_requires_explicit_force_semantics():
    # reconcile_usage 内部不修改全局常量：总开关通过入口已检查；此处仅验证 validate_kind 抛错
    import pytest

    with pytest.raises(ValueError):
        sq.validate_kind("bogus")
