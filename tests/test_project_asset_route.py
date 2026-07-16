from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import files as file_routes
from app.services import paths


class FakeResult:
    def __init__(self, owner_id):
        self.owner_id = owner_id

    def scalar_one_or_none(self):
        return self.owner_id


class FakeSession:
    def __init__(self, owner_id):
        self.owner_id = owner_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, statement):
        return FakeResult(self.owner_id)


class FakeSessionFactory:
    def __init__(self, owner_id):
        self.owner_id = owner_id

    def __call__(self):
        return FakeSession(self.owner_id)


def test_project_asset_route_resolves_owner_storage_label(tmp_path, monkeypatch):
    base_dir = tmp_path / "project_assets"
    asset = base_dir / "user_owner@example.com" / "7" / "covers" / "preview.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"preview-bytes")

    monkeypatch.setattr(paths, "STORAGE_PROJECT_ASSETS_DIR", base_dir)
    monkeypatch.setattr(paths, "get_project_assets_dir", lambda: base_dir)
    monkeypatch.setattr(paths, "iter_known_project_asset_dirs", lambda: [])
    monkeypatch.setattr(file_routes, "async_session", FakeSessionFactory(owner_id=42))
    monkeypatch.setattr(file_routes, "_resolve_runtime_user_id_from_request", lambda request: 42)

    async def resolve_storage_label(user_id):
        assert user_id == 42
        return "user_owner@example.com"

    monkeypatch.setattr(file_routes, "resolve_user_storage_label", resolve_storage_label)

    access_checks = []

    async def ensure_project_access(project_id, user_id):
        access_checks.append((project_id, user_id))

    app = FastAPI()
    app.include_router(file_routes.create_files_router(ensure_project_access, lambda: tmp_path))

    response = TestClient(app).get("/api/project_assets/7/covers/preview.jpg")

    assert response.status_code == 200
    assert response.content == b"preview-bytes"
    assert response.headers["content-type"].startswith("image/jpeg")
    assert access_checks == [(7, 42)]


def test_temp_lut_route_resolves_request_storage_label(tmp_path, monkeypatch):
    asset = tmp_path / "user_owner@example.com" / "result_preview_session123.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"preview")

    monkeypatch.setattr(file_routes, "_resolve_runtime_user_id_from_request", lambda request: 42)

    async def resolve_storage_label(user_id):
        assert user_id == 42
        return "user_owner@example.com"

    monkeypatch.setattr(file_routes, "resolve_user_storage_label", resolve_storage_label)

    async def ensure_project_access(project_id, user_id):
        raise AssertionError("project access is not used for temp LUT previews")

    def runtime_temp_lut_dir(storage_label=None):
        return tmp_path / storage_label if storage_label else tmp_path

    app = FastAPI()
    app.include_router(file_routes.create_files_router(ensure_project_access, runtime_temp_lut_dir))

    response = TestClient(app).get("/temp_luts/result_preview_session123.jpg")

    assert response.status_code == 200
    assert response.content == b"preview"
