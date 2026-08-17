from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import paths


def patch_storage_roots(monkeypatch, tmp_path):
    storage = tmp_path / "storage"
    project_assets = storage / "projects" / "assets"
    temp = storage / "temp"
    users = storage / "users"
    training = storage / "training" / "corpus"

    monkeypatch.setattr(paths, "STORAGE_PROJECT_ASSETS_DIR", project_assets)
    monkeypatch.setattr(paths, "STORAGE_TEMP_DIR", temp)
    monkeypatch.setattr(paths, "STORAGE_USERS_DIR", users)
    monkeypatch.setattr(paths, "STORAGE_TRAINING_CORPUS_DIR", training)
    monkeypatch.setattr(paths, "get_project_assets_dir", lambda: project_assets)
    monkeypatch.setattr(paths, "get_user_assets_dir", lambda: users)
    monkeypatch.setattr(paths, "get_user_images_dir", lambda: users / "local_user" / "images")
    monkeypatch.setattr(paths, "get_user_references_dir", lambda: users / "local_user" / "references")
    monkeypatch.setattr(paths, "get_user_profiles_dir", lambda: users / "local_user" / "profiles")
    monkeypatch.setattr(paths, "iter_known_project_asset_dirs", lambda: iter(()))
    return {
        "storage": storage,
        "project_assets": project_assets,
        "temp": temp,
        "users": users,
        "training": training,
    }


def test_project_asset_new_path_and_url_hide_storage_label(monkeypatch, tmp_path):
    roots = patch_storage_roots(monkeypatch, tmp_path)
    bucket_dir = paths._safe_project_bucket_dir(
        12,
        "source",
        storage_label="user_admin@example.com",
    )
    target = bucket_dir / "a.jpg"
    target.write_bytes(b"image")

    assert target == roots["project_assets"] / "user_admin@example.com" / "12" / "source" / "a.jpg"

    path, url = paths._project_bucket_file(
        12,
        "source",
        "a.jpg",
        storage_label="user_admin@example.com",
    )
    assert path == target
    assert url == "/api/project_assets/12/source/a.jpg"
    assert "user_admin@example.com" not in url

    resolved = paths._safe_project_asset_file(
        12,
        "source/a.jpg",
        storage_label="user_admin@example.com",
    )
    assert resolved == target


def test_project_asset_legacy_user_dir_requires_explicit_scan(monkeypatch, tmp_path):
    roots = patch_storage_roots(monkeypatch, tmp_path)
    legacy = roots["project_assets"] / "user_other@example.com" / "12" / "source" / "old.jpg"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"old")

    with pytest.raises(HTTPException):
        paths._safe_project_asset_file(
            12,
            "source/old.jpg",
            storage_label="user_test@example.com",
        )

    assert paths._safe_project_asset_file(
        12,
        "source/old.jpg",
        storage_label="user_test@example.com",
        scan_legacy_user_dirs=True,
    ) == legacy


def test_runtime_user_temp_new_path_and_user_check(monkeypatch, tmp_path):
    roots = patch_storage_roots(monkeypatch, tmp_path)
    target = paths._save_to_runtime_user_temp(
        b"data",
        1,
        "a.jpg",
        storage_label="user_test@example.com",
    )

    assert target == roots["temp"] / "user_uploads" / "user_test@example.com" / "a.jpg"
    assert paths._runtime_user_temp_url(1, "a.jpg") == "/api/user_temp/1/a.jpg"

    resolved = paths._resolve_local_file_path(
        "/api/user_temp/1/a.jpg",
        request_user_id=1,
        request_storage_label="user_test@example.com",
    )
    assert resolved == target

    denied = paths._resolve_local_file_path(
        "/api/user_temp/2/a.jpg",
        request_user_id=1,
        request_storage_label="user_test@example.com",
    )
    assert denied is None


def test_runtime_user_temp_old_user_id_dir_fallback(monkeypatch, tmp_path):
    roots = patch_storage_roots(monkeypatch, tmp_path)
    old_path = roots["temp"] / "user_uploads" / "user_1" / "old.jpg"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old")

    resolved = paths._safe_runtime_user_temp_file(
        1,
        "old.jpg",
        storage_label="user_test@example.com",
    )
    assert resolved == old_path


def test_user_assets_new_path_and_old_fallback(monkeypatch, tmp_path):
    roots = patch_storage_roots(monkeypatch, tmp_path)
    new_avatar = roots["users"] / "user_test@example.com" / "profiles" / "avatars" / "a.jpg"
    new_avatar.parent.mkdir(parents=True, exist_ok=True)
    new_avatar.write_bytes(b"new")

    assert paths._safe_user_asset_file(
        "profiles",
        "avatars/a.jpg",
        storage_label="user_test@example.com",
        user_id=1,
    ) == new_avatar

    old_image = roots["users"] / "user_1" / "images" / "old.jpg"
    old_image.parent.mkdir(parents=True, exist_ok=True)
    old_image.write_bytes(b"old")

    assert paths._safe_user_asset_file(
        "images",
        "old.jpg",
        storage_label="user_missing@example.com",
        user_id=1,
    ) == old_image


def test_training_corpus_dir_for_label(monkeypatch, tmp_path):
    roots = patch_storage_roots(monkeypatch, tmp_path)
    assert (
        paths._training_corpus_dir_for_label("user_test@example.com")
        == roots["training"] / "user_test@example.com"
    )


def test_invalid_storage_label_and_path_traversal_rejected(monkeypatch, tmp_path):
    patch_storage_roots(monkeypatch, tmp_path)

    with pytest.raises(ValueError):
        paths._runtime_user_temp_dir_for_label("../x")

    with pytest.raises(HTTPException):
        paths._safe_runtime_user_temp_file(
            1,
            "../secret.jpg",
            storage_label="user_test@example.com",
        )

    assert (
        paths._resolve_local_file_path(
            "/api/user_temp/1/../secret.jpg",
            request_user_id=1,
            request_storage_label="user_test@example.com",
        )
        is None
    )


def test_workspace_path_requires_explicit_opt_in(monkeypatch, tmp_path):
    patch_storage_roots(monkeypatch, tmp_path)
    local_file = tmp_path / "workspace-image.jpg"
    local_file.write_bytes(b"image")
    monkeypatch.setattr(paths, "BASE_DIR", tmp_path)

    assert paths._resolve_local_file_path(str(local_file)) is None
    assert (
        paths._resolve_local_file_path(
            str(local_file),
            allow_workspace_path=True,
        )
        == local_file
    )
