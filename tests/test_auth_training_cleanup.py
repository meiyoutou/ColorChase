import json

from app.routes import auth as auth_routes
from app.services import paths
from models import User


def _write_meta(sample_dir, data):
    sample_dir.mkdir(parents=True, exist_ok=True)
    (sample_dir / "meta.json").write_text(json.dumps(data), encoding="utf-8")
    (sample_dir / "target.jpg").write_bytes(b"target")


def test_account_delete_collects_current_and_legacy_training_corpus_paths(monkeypatch, tmp_path):
    training_root = tmp_path / "storage" / "training" / "corpus"
    monkeypatch.setattr(paths, "STORAGE_TRAINING_CORPUS_DIR", training_root)
    monkeypatch.setattr(auth_routes, "STORAGE_TRAINING_CORPUS_DIR", training_root)

    current = training_root / "user_admin@example.com" / "sample_current"
    fallback = training_root / "user_7" / "sample_fallback"
    legacy_email = training_root / "admin_example_com" / "sample_legacy_email"
    nested_legacy = training_root / "high_rating" / "user_admin@example.com" / "sample_nested"
    other_user = training_root / "high_rating" / "user_other@example.com" / "sample_other"

    _write_meta(current, {"storage_label": "user_admin@example.com", "user_id": 7})
    _write_meta(fallback, {"user_folder": "user_7", "user_id": 7})
    _write_meta(legacy_email, {"user_folder": "admin_example_com"})
    _write_meta(nested_legacy, {"storage_label": "user_admin@example.com", "user_id": 7})
    _write_meta(other_user, {"storage_label": "user_other@example.com", "user_id": 8})

    user = User(id=7, email="admin@example.com", storage_label="user_admin@example.com")
    cleanup_paths = set()

    auth_routes._add_training_corpus_cleanup_paths(user, "user_admin@example.com", cleanup_paths)
    for path in sorted(cleanup_paths, key=lambda p: len(str(p)), reverse=True):
        auth_routes._remove_if_exists(path)

    assert not current.exists()
    assert not fallback.exists()
    assert not legacy_email.exists()
    assert not nested_legacy.exists()
    assert other_user.exists()
