import json
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
MODEL_ASSETS_DIR = BASE_DIR / "model_assets"
MODEL_DIR = MODEL_ASSETS_DIR
LEGACY_MODEL_DIR = BASE_DIR / "models"
WEIGHTS_DIR = BASE_DIR / "weights"
ARTIFACTS_MODEL_DIR = BASE_DIR / "artifacts" / "models"
STATIC_DIR = BASE_DIR / "static"
STORAGE_DIR = BASE_DIR / "storage"
STORAGE_PROJECTS_DIR = STORAGE_DIR / "projects"
STORAGE_PROJECT_ASSETS_DIR = STORAGE_PROJECTS_DIR / "assets"
STORAGE_UPLOADS_DIR = STORAGE_DIR / "uploads"
STORAGE_IMAGE_UPLOADS_DIR = STORAGE_UPLOADS_DIR / "images"
STORAGE_VIDEO_UPLOADS_DIR = STORAGE_UPLOADS_DIR / "videos"
STORAGE_VIDEOS_DIR = STORAGE_DIR / "videos"
STORAGE_TEMP_DIR = STORAGE_DIR / "temp"
STORAGE_IMAGE_LUT_DIR = STORAGE_TEMP_DIR / "luts"
STORAGE_VIDEO_FRAMES_DIR = STORAGE_TEMP_DIR / "frames"
STORAGE_LOGS_DIR = STORAGE_DIR / "logs"
STORAGE_IMAGE_DEBUG_DIR = STORAGE_LOGS_DIR / "debug_output"
STORAGE_USERS_DIR = STORAGE_DIR / "users"
STORAGE_USER_LOCAL_DIR = STORAGE_USERS_DIR / "local_user"
STORAGE_USER_IMAGES_DIR = STORAGE_USER_LOCAL_DIR / "images"
STORAGE_USER_REFERENCES_DIR = STORAGE_USER_LOCAL_DIR / "references"
STORAGE_USER_PROFILES_DIR = STORAGE_USER_LOCAL_DIR / "profiles"
STORAGE_TRAINING_DIR = STORAGE_DIR / "training"
STORAGE_TRAINING_CORPUS_DIR = STORAGE_TRAINING_DIR / "corpus"
STORAGE_CACHE_DIR = STORAGE_DIR / "cache"
STORAGE_STYLES_DIR = STORAGE_DIR / "styles"
STORAGE_STYLES_EXTRACTED_DIR = STORAGE_STYLES_DIR / "extracted"

USER_CONFIG_PATH = BASE_DIR / "user_config.json"
USER_CONFIG_DIR = BASE_DIR / "user_configs"
LEGACY_CACHE_DIR = BASE_DIR / ".cache"
LEGACY_DEFAULT_PATHS = {
    "project_assets": BASE_DIR / "user_assets" / "projects",
    "image_uploads": BASE_DIR / "uploads",
    "image_luts": BASE_DIR / "temp_luts",
    "image_debug": BASE_DIR / "debug_output",
    "video_uploads": BASE_DIR / "uploads",
    "video_results": BASE_DIR / "videos",
    "video_frames": BASE_DIR / "temp_frames",
}

DEFAULT_PATHS = {
    "project_assets": str(STORAGE_PROJECT_ASSETS_DIR),
    "image_uploads": str(STORAGE_IMAGE_UPLOADS_DIR),
    "image_luts": str(STORAGE_IMAGE_LUT_DIR),
    "image_debug": str(STORAGE_IMAGE_DEBUG_DIR),
    "video_uploads": str(STORAGE_VIDEO_UPLOADS_DIR),
    "video_results": str(STORAGE_VIDEOS_DIR),
    "video_frames": str(STORAGE_VIDEO_FRAMES_DIR),
}
RUNTIME_PATH_KEYS = tuple(DEFAULT_PATHS.keys())

_RUNTIME_USER_ID: ContextVar[Optional[int]] = ContextVar("runtime_user_id", default=None)


def _coerce_user_id(user_id: Optional[int]) -> Optional[int]:
    if user_id in (None, "", False):
        return None
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return None


def set_current_runtime_user(user_id: Optional[int]):
    return _RUNTIME_USER_ID.set(_coerce_user_id(user_id))


def reset_current_runtime_user(token) -> None:
    _RUNTIME_USER_ID.reset(token)


def get_current_runtime_user() -> Optional[int]:
    return _RUNTIME_USER_ID.get()


def _resolve_user_id(user_id: Optional[int] = None) -> Optional[int]:
    explicit = _coerce_user_id(user_id)
    if explicit is not None:
        return explicit
    return get_current_runtime_user()


def _config_path(user_id: Optional[int] = None) -> Path:
    resolved_user_id = _resolve_user_id(user_id)
    if resolved_user_id is None:
        return USER_CONFIG_PATH
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return USER_CONFIG_DIR / f"user_{resolved_user_id}.json"


def _load_config(user_id: Optional[int] = None):
    config_path = _config_path(user_id)
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                return loaded if isinstance(loaded, dict) else {}
        except Exception:
            pass
    return {}


def _is_legacy_default_path(key: str, value) -> bool:
    legacy = LEGACY_DEFAULT_PATHS.get(key)
    if legacy is None or value in (None, ""):
        return False
    try:
        return _normalize_path(value) == _normalize_path(legacy)
    except Exception:
        return False


def _upgrade_legacy_config_paths(cfg: dict) -> dict:
    upgraded = dict(cfg or {})
    for key, default_value in DEFAULT_PATHS.items():
        if _is_legacy_default_path(key, upgraded.get(key)):
            upgraded[key] = default_value
    return upgraded


def get_user_config(user_id: Optional[int] = None):
    cfg = dict(DEFAULT_PATHS)
    cfg.update(_load_config(user_id))
    return _upgrade_legacy_config_paths(cfg)


def save_user_config(data: dict, user_id: Optional[int] = None):
    current = dict(DEFAULT_PATHS)
    current.update(data)
    current = _upgrade_legacy_config_paths(current)
    config_path = _config_path(user_id)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)


def _normalize_path(value) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate.resolve()


def _path(key: str, user_id: Optional[int] = None) -> Path:
    cfg = get_user_config(user_id)
    configured = cfg.get(key, DEFAULT_PATHS[key])
    resolved = _normalize_path(configured)
    resolved_user_id = _resolve_user_id(user_id)
    # 按用户隔离：如果使用的是默认路径（没自定义过），且当前有 user_id，
    # 就在默认路径下加 user_{邮箱} 子目录，避免多个管理员/用户的数据混在一起。
    # 用户自定义路径不加，尊重用户的自定义配置。
    if resolved_user_id is not None:
        default_resolved = _normalize_path(DEFAULT_PATHS[key])
        if resolved == default_resolved:
            return default_resolved / _resolve_user_label(resolved_user_id)
    return resolved


def _resolve_user_email(user_id: int) -> Optional[str]:
    """兼容旧调用：路径配置层不再查询数据库。"""
    return None
            # 优先用邮箱，邮箱为空时用手机号


    """把邮箱转成安全的目录名，加上 user_ 前缀。"""


def _resolve_user_label(user_id: int) -> str:
    """路径配置层只使用 user_id，不查询数据库。"""
    return f"user_{user_id}"


def get_runtime_paths(user_id: Optional[int] = None):
    return {key: _path(key, user_id) for key in RUNTIME_PATH_KEYS}


def get_current_runtime_path_strings(user_id: Optional[int] = None):
    return {key: str(path) for key, path in get_runtime_paths(user_id).items()}


def get_image_upload_dir(user_id: Optional[int] = None) -> Path:
    return _path("image_uploads", user_id)


def get_user_assets_dir(user_id: Optional[int] = None) -> Path:
    return STORAGE_USERS_DIR


def get_user_local_dir(user_id: Optional[int] = None) -> Path:
    return STORAGE_USER_LOCAL_DIR


def get_user_images_dir(user_id: Optional[int] = None) -> Path:
    return STORAGE_USER_IMAGES_DIR


def get_user_references_dir(user_id: Optional[int] = None) -> Path:
    return STORAGE_USER_REFERENCES_DIR


def get_user_profiles_dir(user_id: Optional[int] = None) -> Path:
    return STORAGE_USER_PROFILES_DIR


def get_project_assets_dir(user_id: Optional[int] = None) -> Path:
    return _path("project_assets", user_id)


def get_image_lut_dir(user_id: Optional[int] = None) -> Path:
    return _path("image_luts", user_id)


def get_image_debug_dir(user_id: Optional[int] = None) -> Path:
    return _path("image_debug", user_id)


def get_video_upload_dir(user_id: Optional[int] = None) -> Path:
    return _path("video_uploads", user_id)


def get_video_result_dir(user_id: Optional[int] = None) -> Path:
    return _path("video_results", user_id)


def get_video_frames_dir(user_id: Optional[int] = None) -> Path:
    return _path("video_frames", user_id)


def get_training_corpus_dir(user_id: Optional[int] = None) -> Path:
    return STORAGE_TRAINING_CORPUS_DIR


def get_storage_cache_dir(user_id: Optional[int] = None) -> Path:
    return STORAGE_CACHE_DIR


def get_upload_dir(user_id: Optional[int] = None) -> Path:
    return get_image_upload_dir(user_id)


def get_video_dir(user_id: Optional[int] = None) -> Path:
    return get_video_result_dir(user_id)


def get_temp_lut_dir(user_id: Optional[int] = None) -> Path:
    return get_image_lut_dir(user_id)


def ensure_runtime_dirs(user_id: Optional[int] = None):
    paths = get_runtime_paths(user_id)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    for extra in (
        STORAGE_DIR,
        STORAGE_PROJECTS_DIR,
        STORAGE_UPLOADS_DIR,
        STORAGE_VIDEO_UPLOADS_DIR,
        STORAGE_VIDEOS_DIR,
        STORAGE_TEMP_DIR,
        STORAGE_LOGS_DIR,
        STORAGE_USERS_DIR,
        STORAGE_USER_LOCAL_DIR,
        STORAGE_TRAINING_CORPUS_DIR,
        STORAGE_CACHE_DIR,
        STORAGE_STYLES_DIR,
        STORAGE_STYLES_EXTRACTED_DIR,
    ):
        extra.mkdir(parents=True, exist_ok=True)
    return paths


def _iter_user_subdirs(parent: Path):
    """扫描某个父目录下所有 user_{id} 子目录（按用户隔离后的新结构）。"""
    try:
        if not parent.exists():
            return
        for item in parent.iterdir():
            if item.is_dir() and item.name.startswith("user_"):
                yield item
    except Exception:
        return


def iter_known_video_dirs():
    seen = set()

    def _append(path_value):
        try:
            resolved = _normalize_path(path_value)
        except Exception:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        yield resolved

    # 旧全局目录（兼容历史数据）
    for item in _append(DEFAULT_PATHS["video_results"]):
        yield item
    for item in _append(LEGACY_DEFAULT_PATHS["video_results"]):
        yield item
    # 新按用户子目录
    for user_dir in _iter_user_subdirs(_normalize_path(DEFAULT_PATHS["video_results"])):
        for item in _append(user_dir):
            yield item

    for config_path in [USER_CONFIG_PATH] + sorted(USER_CONFIG_DIR.glob("*.json")) if USER_CONFIG_DIR.exists() else [USER_CONFIG_PATH]:
        if not config_path.exists():
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        video_dir = raw.get("video_results")
        if not video_dir:
            continue
        for item in _append(video_dir):
            yield item


def iter_known_training_dirs():
    seen = set()

    def _append(path_value):
        try:
            resolved = _normalize_path(path_value)
        except Exception:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        yield resolved

    # 只保留统一目录，旧目录已在启动时迁移并删除
    for item in _append(STORAGE_TRAINING_CORPUS_DIR):
        yield item


def iter_known_project_asset_dirs():
    seen = set()

    def _append(path_value):
        try:
            resolved = _normalize_path(path_value)
        except Exception:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        yield resolved

    # 旧全局目录（兼容历史数据）
    for item in _append(DEFAULT_PATHS["project_assets"]):
        yield item
    for item in _append(LEGACY_DEFAULT_PATHS["project_assets"]):
        yield item
    for item in _append(BASE_DIR / "uploaded" / "projects"):
        yield item
    # 新按用户子目录
    for user_dir in _iter_user_subdirs(_normalize_path(DEFAULT_PATHS["project_assets"])):
        for item in _append(user_dir):
            yield item

    config_paths = [USER_CONFIG_PATH]
    if USER_CONFIG_DIR.exists():
        config_paths += sorted(USER_CONFIG_DIR.glob("*.json"))
    for config_path in config_paths:
        if not config_path.exists():
            continue
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        project_dir = raw.get("project_assets")
        if not project_dir:
            continue
        for item in _append(project_dir):
            yield item


def iter_known_user_asset_dirs():
    seen = set()

    def _append(path_value):
        try:
            resolved = _normalize_path(path_value)
        except Exception:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        yield resolved

    for item in _append(STORAGE_USERS_DIR):
        yield item
    for item in _append(BASE_DIR / "user_assets"):
        yield item


def iter_known_style_dirs():
    seen = set()

    def _append(path_value):
        try:
            resolved = _normalize_path(path_value)
        except Exception:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        yield resolved

    for item in _append(STORAGE_STYLES_DIR):
        yield item
    for item in _append(BASE_DIR / "styles"):
        yield item


def iter_known_style_extracted_dirs():
    seen = set()

    def _append(path_value):
        try:
            resolved = _normalize_path(path_value)
        except Exception:
            return
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        yield resolved

    for item in _append(STORAGE_STYLES_EXTRACTED_DIR):
        yield item
    for item in _append(BASE_DIR / "styles" / "extracted"):
        yield item


# 兼容旧代码的初始化别名，仅用于历史调用；新的读写请使用 getter。
IMAGE_UPLOAD_DIR = get_image_upload_dir()
IMAGE_LUT_DIR = get_image_lut_dir()
IMAGE_DEBUG_DIR = get_image_debug_dir()
VIDEO_UPLOAD_DIR = get_video_upload_dir()
VIDEO_RESULT_DIR = get_video_result_dir()
VIDEO_FRAMES_DIR = get_video_frames_dir()
UPLOAD_DIR = get_upload_dir()
VIDEO_DIR = get_video_dir()
TEMP_LUT_DIR = get_temp_lut_dir()

CACHE_DIR = STORAGE_CACHE_DIR

MODFLOWS_MODEL_DIR = MODEL_DIR / "modflows"
NEURALPRESET_MODEL_DIR = MODEL_DIR / "neural_preset"
NEURALPRESET_MODEL_DIR_ALIASES = (
    NEURALPRESET_MODEL_DIR,
    MODEL_DIR / "neuralpreset",
    LEGACY_MODEL_DIR / "neural_preset",
    LEGACY_MODEL_DIR / "neuralpreset",
    WEIGHTS_DIR / "neural_preset",
    WEIGHTS_DIR / "neuralpreset",
    ARTIFACTS_MODEL_DIR / "neural_preset",
    ARTIFACTS_MODEL_DIR / "neuralpreset",
)

MODFLOWS_B6_CHECKPOINT = MODFLOWS_MODEL_DIR / "modflows_color_encoder_B6_dim_8195_iter_700000.pt"
MODFLOWS_B0_CHECKPOINT = MODFLOWS_MODEL_DIR / "modflows_color_encoder_B0_dim_515.pt"

NEURALPRESET_STYLE_ONNX = NEURALPRESET_MODEL_DIR / "StyleSimiliaryDiscriminator.onnx"
NEURALPRESET_LDC_WEIGHTS = NEURALPRESET_MODEL_DIR / "ldc.pth"


def iter_neuralpreset_model_dirs():
    seen = set()
    for path in NEURALPRESET_MODEL_DIR_ALIASES:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield path


def iter_known_model_dirs():
    seen = set()
    for path in (
        MODEL_ASSETS_DIR,
        LEGACY_MODEL_DIR,
        WEIGHTS_DIR,
        ARTIFACTS_MODEL_DIR,
    ):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield path


def resolve_model_asset_path(*parts: str) -> Optional[Path]:
    for root in iter_known_model_dirs():
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return None


def find_neuralpreset_weight(filename: str) -> Path:
    for model_dir in iter_neuralpreset_model_dirs():
        candidate = model_dir / filename
        if candidate.exists():
            return candidate
    return NEURALPRESET_MODEL_DIR / filename


def get_neuralpreset_weight_status():
    norm_path = find_neuralpreset_weight("norm_stage_best.pth")
    style_path = find_neuralpreset_weight("style_stage_best.pth")
    missing = []
    if not norm_path.exists():
        missing.append("norm_stage_best.pth")
    if not style_path.exists():
        missing.append("style_stage_best.pth")
    return {
        "model_dirs": [str(path) for path in iter_neuralpreset_model_dirs()],
        "norm_path": norm_path,
        "style_path": style_path,
        "ready": not missing,
        "missing": missing,
    }


NEURALPRESET_NORM_WEIGHTS = find_neuralpreset_weight("norm_stage_best.pth")
NEURALPRESET_STYLE_WEIGHTS = find_neuralpreset_weight("style_stage_best.pth")
