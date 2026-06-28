import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from core.io.loaders import FORMAT_INFO, SUPPORTED_EXTENSIONS


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def create_meta_router(base_dir: Path, algorithms: dict):
    router = APIRouter()

    @router.get("/")
    async def index():
        return FileResponse(os.path.join(str(base_dir), "static", "index.html"))

    @router.get("/api/formats")
    async def api_formats():
        return JSONResponse(
            {
                "formats": _json_safe(FORMAT_INFO),
                "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
            }
        )

    @router.get("/api/algorithms")
    async def api_algorithms():
        return JSONResponse(_json_safe(algorithms))

    return router
