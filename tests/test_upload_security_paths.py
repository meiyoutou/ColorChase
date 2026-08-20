from app.security import (
    _is_ai_limited_path,
    _is_image_original_upload_path,
    _is_upload_limited_path,
)


def test_training_and_detection_upload_paths_are_rate_and_size_limited():
    # 2026-08 修复：这两个是前端实际调用的上传接口（training.py:279,244），
    # 此前只有超级管理员专用的 /api/train/upload 在清单里，导致真正暴露给
    # 所有登录用户的两个接口完全没有限流和大小限制。
    assert _is_upload_limited_path("/api/training/upload")
    assert _is_upload_limited_path("/api/detection/upload")
    assert _is_image_original_upload_path("/api/training/upload")
    assert _is_image_original_upload_path("/api/detection/upload")


def test_super_admin_train_upload_path_still_limited():
    assert _is_upload_limited_path("/api/train/upload")
    assert _is_image_original_upload_path("/api/train/upload")


def test_unrelated_paths_are_not_upload_limited():
    assert not _is_upload_limited_path("/api/train/samples")
    assert not _is_upload_limited_path("/api/health")


def test_benchmark_paths_are_ai_limited():
    assert _is_ai_limited_path("/api/admin/models/modflows/benchmark")
    assert not _is_ai_limited_path("/api/admin/models/modflows/status")
