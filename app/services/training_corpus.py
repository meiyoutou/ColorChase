import base64
import hashlib
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select

from app.routes.projects import _read_project_snapshot
from app.settings import USER_SPACE_TZ
from config import BASE_DIR, get_project_assets_dir, get_training_corpus_dir
from database import async_session
from models import Asset, Project, User

TRAINING_CORPUS_DIR = get_training_corpus_dir()
TRAINING_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def resolve_training_dir(image_dir: str) -> Path:
    image_dir = (image_dir or "").strip()
    if not image_dir:
        raise HTTPException(status_code=400, detail="训练数据目录不能为空")
    candidate = Path(image_dir)
    if not candidate.is_absolute():
        candidate = BASE_DIR / image_dir
    return candidate.resolve()


def get_training_data_stats_payload(image_dir: str):
    training_path = resolve_training_dir(image_dir)
    file_count = 0
    total_size = 0
    if training_path.exists() and training_path.is_dir():
        for file_path in training_path.iterdir():
            if not file_path.is_file() or file_path.suffix.lower() not in TRAINING_IMAGE_EXTENSIONS:
                continue
            file_count += 1
            total_size += file_path.stat().st_size
    return {
        "image_dir": str(training_path),
        "training_file_count": file_count,
        "training_size_mb": round(total_size / (1024 * 1024), 1),
    }


def _safe_training_name(value: str, fallback: str) -> str:
    raw = str(value or "").strip() or fallback
    cleaned = []
    for ch in raw:
        if ch.isalnum() or ch in "@._+-":
            cleaned.append(ch)
        else:
            cleaned.append("_")
    cleaned = "".join(cleaned).strip("._-")[:96]
    return cleaned or fallback


def _resolve_training_source_path(resolve_local_file_path, value: str) -> Optional[Path]:
    resolved = resolve_local_file_path(value)
    if not resolved or not resolved.is_file():
        return None
    return resolved


async def _training_user_folder_name(user_id: Optional[int]) -> str:
    if not user_id:
        return "anonymous"
    try:
        async with async_session() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user:
                identity = user.email or user.phone or f"user_{user.id}"
                return _safe_training_name(identity, f"user_{user.id}")
    except Exception as exc:
        print(f"[Training Corpus] resolve user failed: {exc}")
    return f"user_{user_id}"


def _copy_training_file(src: Optional[Path], dst_dir: Path, label: str) -> str:
    if not src:
        return ""
    ext = src.suffix.lower() or ".jpg"
    dst = dst_dir / f"{label}{ext}"
    shutil.copy2(src, dst)
    return str(dst)


def _training_sample_id_for_target(resolve_local_file_path, target_path: str) -> str:
    src = _resolve_training_source_path(resolve_local_file_path, target_path)
    if src:
        try:
            stat = src.stat()
            sig = f"{src.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            sig = str(src)
    else:
        sig = str(target_path or uuid.uuid4().hex)
    return "target_" + hashlib.sha1(sig.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _read_training_meta(sample_dir: Path) -> dict:
    meta_path = sample_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _ensure_training_target_sample(
    resolve_local_file_path,
    *,
    user_id: Optional[int],
    target_path: str,
    project_id: int = 0,
    asset_name: str = "",
) -> Optional[Path]:
    target_src = _resolve_training_source_path(resolve_local_file_path, target_path)
    if not target_src:
        return None

    user_folder = await _training_user_folder_name(user_id)
    sample_id = _training_sample_id_for_target(resolve_local_file_path, target_path)
    sample_dir = TRAINING_CORPUS_DIR / user_folder / sample_id
    sample_dir.mkdir(parents=True, exist_ok=True)

    target_copy = ""
    existing = next(sample_dir.glob("target.*"), None)
    if existing and existing.is_file():
        target_copy = str(existing)
    else:
        target_copy = _copy_training_file(target_src, sample_dir, "target")

    meta = _read_training_meta(sample_dir)
    meta.update({
        "sample_id": sample_id,
        "user_folder": user_folder,
        "user_id": user_id,
        "project_id": project_id or meta.get("project_id") or 0,
        "asset_name": asset_name or meta.get("asset_name") or target_src.name,
        "target_path": str(target_src),
        "files": {
            **(meta.get("files") if isinstance(meta.get("files"), dict) else {}),
            "target": target_copy,
        },
        "target_created_at": meta.get("target_created_at") or datetime.now(USER_SPACE_TZ).isoformat(),
        "updated_at": datetime.now(USER_SPACE_TZ).isoformat(),
    })
    (sample_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return sample_dir


def _write_training_data_url(data_url: str, dst_dir: Path, label: str) -> str:
    raw = str(data_url or "").strip()
    if not raw.startswith("data:image/") or "," not in raw:
        return ""
    header, payload = raw.split(",", 1)
    mime = header.split(";", 1)[0].replace("data:", "").lower()
    ext_map = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    ext = ext_map.get(mime, ".jpg")
    try:
        content = base64.b64decode(payload)
    except Exception:
        return ""
    if not content:
        return ""
    dst = dst_dir / f"{label}{ext}"
    dst.write_bytes(content)
    return str(dst)


def _find_sample_file(sample_dir: Path, label: str) -> str:
    if not sample_dir.exists():
        return ""
    for path in sorted(sample_dir.glob(f"{label}.*")):
        if path.is_file():
            return str(path)
    return ""


def _find_project_snapshot_image(resolve_local_file_path, snapshot: dict, item: dict, *, kind: str, project_id: int) -> str:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    item = item if isinstance(item, dict) else {}
    candidates = []
    if kind == "reference":
        candidates.extend([
            item.get("localReferencePath"),
            item.get("refDataUrl"),
            item.get("refSavedPath"),
            snapshot.get("refDataUrl"),
            snapshot.get("refSavedPath"),
            snapshot.get("videoRefSavedPath"),
        ])
    elif kind == "result":
        candidates.extend([
            item.get("localResultPath"),
            item.get("resultDataUrl"),
            item.get("resultSavedPath"),
            snapshot.get("videoResultUrl"),
            snapshot.get("resultDataUrl"),
        ])
    for candidate in candidates:
        resolved = _resolve_training_source_path(resolve_local_file_path, candidate)
        if resolved and resolved.is_file():
            return str(resolved)
    if project_id > 0:
        project_root = get_project_assets_dir() / str(project_id)
        for folder in ("reference", "result", "exports"):
            folder_dir = project_root / folder
            if not folder_dir.exists():
                continue
            for file_path in sorted(folder_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                if kind == "reference" and "reference" in file_path.parent.name.lower():
                    return str(file_path)
                if kind == "result" and (
                    "result" in file_path.parent.name.lower()
                    or "export" in file_path.parent.name.lower()
                    or "result" in file_path.name.lower()
                ):
                    return str(file_path)
    return ""


def _find_snapshot_target_item(snapshot: dict, meta: dict) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    items = snapshot.get("targetImages") if isinstance(snapshot.get("targetImages"), list) else []
    target_name = str(meta.get("asset_name") or "").strip().lower()
    target_path = str(meta.get("target_path") or "").strip().lower()
    target_basename = Path(target_path).name.lower() if target_path else ""
    for item in items:
        if not isinstance(item, dict):
            continue
        for candidate in (
            item.get("name"),
            item.get("savedPath"),
            item.get("sourcePath"),
            item.get("thumbnailUrl"),
            item.get("localSourcePath"),
        ):
            raw = str(candidate or "").strip().lower()
            if raw and (raw == target_name or raw == target_path or Path(raw).name.lower() == target_basename):
                return item
    return {}


async def _load_project_snapshot(project_id: int) -> dict:
    if project_id <= 0:
        return {}
    try:
        async with async_session() as session:
            result = await session.execute(select(Project).where(Project.id == project_id))
            project = result.scalar_one_or_none()
            if not project or not project.workspace_snapshot:
                return {}
            return _read_project_snapshot(project.workspace_snapshot)
    except Exception as exc:
        print(f"[Training Corpus] load snapshot failed: {exc}")
        return {}


async def _backfill_training_sample(resolve_local_file_path, sample_dir: Path) -> bool:
    meta = _read_training_meta(sample_dir)
    if not meta:
        return False

    files_meta = meta.get("files") if isinstance(meta.get("files"), dict) else {}
    project_id = int(meta.get("project_id") or 0)
    snapshot = await _load_project_snapshot(project_id)
    snapshot_item = _find_snapshot_target_item(snapshot, meta)

    changed = False
    target_copy = _find_sample_file(sample_dir, "target") or str(files_meta.get("target") or "")
    reference_copy = _find_sample_file(sample_dir, "reference") or str(files_meta.get("reference") or "")
    result_copy = _find_sample_file(sample_dir, "result") or str(files_meta.get("result") or "")

    if not reference_copy:
        reference_src = _resolve_training_source_path(
            resolve_local_file_path,
            _find_project_snapshot_image(resolve_local_file_path, snapshot, snapshot_item, kind="reference", project_id=project_id),
        )
        if reference_src:
            reference_copy = _copy_training_file(reference_src, sample_dir, "reference")
            changed = bool(reference_copy)

    if not result_copy:
        result_src = _resolve_training_source_path(
            resolve_local_file_path,
            _find_project_snapshot_image(resolve_local_file_path, snapshot, snapshot_item, kind="result", project_id=project_id),
        )
        if result_src:
            result_copy = _copy_training_file(result_src, sample_dir, "result")
            changed = bool(result_copy) or changed

    rating_value = int(meta.get("rating") or 0)
    if rating_value <= 0:
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(Asset).where(
                        Asset.project_id == project_id,
                        Asset.file_name == str(meta.get("asset_name") or "").strip(),
                    )
                )
                asset = result.scalar_one_or_none()
                if asset and int(asset.rating or 0) > 0:
                    rating_value = int(asset.rating or 0)
        except Exception:
            pass

    if changed or rating_value != int(meta.get("rating") or 0):
        meta["rating"] = rating_value
        meta["files"] = {
            **files_meta,
            "target": target_copy,
            "reference": reference_copy,
            "result": result_copy,
        }
        meta["updated_at"] = datetime.now(USER_SPACE_TZ).isoformat()
        (sample_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False


async def backfill_training_corpus(resolve_local_file_path):
    if not TRAINING_CORPUS_DIR.exists():
        return
    for user_dir in TRAINING_CORPUS_DIR.iterdir():
        if not user_dir.is_dir():
            continue
        for sample_dir in user_dir.iterdir():
            if sample_dir.is_dir():
                try:
                    await _backfill_training_sample(resolve_local_file_path, sample_dir)
                except Exception as exc:
                    print(f"[Training Corpus] backfill failed: {sample_dir} -> {exc}")


async def run_startup_training_corpus_backfill(resolve_local_file_path):
    started_at = datetime.now().timestamp()
    try:
        await backfill_training_corpus(resolve_local_file_path)
        elapsed = round(datetime.now().timestamp() - started_at, 2)
        print(f"[Training Corpus] startup backfill finished in {elapsed}s")
    except Exception as exc:
        print(f"[Training Corpus] startup backfill failed: {exc}")


async def _archive_training_sample(
    resolve_local_file_path,
    *,
    user_id: Optional[int],
    project_id: int,
    asset_name: str,
    target_path: str,
    reference_path: str,
    reference_data_url: str,
    result_bytes: bytes,
    result_ext: str,
    rating: int,
    algorithm: str,
    session_id: str,
    merged_session_id: str,
    export_format: str,
    size_mode: str,
    params: dict,
) -> Optional[str]:
    if rating < 1 or rating > 5:
        return None
    sample_dir = await _ensure_training_target_sample(
        resolve_local_file_path,
        user_id=user_id,
        target_path=target_path,
        project_id=project_id,
        asset_name=asset_name,
    )
    if sample_dir is None:
        return None
    meta = _read_training_meta(sample_dir)
    sample_id = meta.get("sample_id") or sample_dir.name
    user_folder = meta.get("user_folder") or sample_dir.parent.name

    target_src = _resolve_training_source_path(resolve_local_file_path, target_path)
    reference_src = _resolve_training_source_path(resolve_local_file_path, reference_path)
    if not reference_src:
        reference_src = _resolve_training_source_path(
            resolve_local_file_path,
            _find_project_snapshot_image(resolve_local_file_path, meta, {}, kind="reference", project_id=project_id),
        )
    files_meta = meta.get("files") if isinstance(meta.get("files"), dict) else {}
    target_copy = files_meta.get("target") or ""
    if not target_copy or not Path(target_copy).exists():
        target_copy = _copy_training_file(target_src, sample_dir, "target")
    reference_copy = _copy_training_file(reference_src, sample_dir, "reference")
    if not reference_copy:
        reference_copy = _write_training_data_url(reference_data_url, sample_dir, "reference")

    result_ext = result_ext if result_ext.startswith(".") else f".{result_ext or 'jpg'}"
    result_path = sample_dir / f"result{result_ext.lower()}"
    result_path.write_bytes(result_bytes)

    now_text = datetime.now(USER_SPACE_TZ).isoformat()
    meta.update({
        "sample_id": sample_id,
        "user_folder": user_folder,
        "user_id": user_id,
        "project_id": project_id,
        "asset_name": asset_name,
        "rating": rating,
        "algorithm": algorithm,
        "session_id": session_id,
        "merged_session_id": merged_session_id,
        "export_format": export_format,
        "size_mode": size_mode,
        "params": params,
        "files": {
            **files_meta,
            "target": target_copy,
            "reference": reference_copy,
            "result": str(result_path),
        },
        "created_at": meta.get("created_at") or meta.get("target_created_at") or now_text,
        "updated_at": now_text,
        "completed_at": now_text,
    })
    (sample_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Training Corpus] archived sample: {sample_dir}")
    return str(sample_dir)
