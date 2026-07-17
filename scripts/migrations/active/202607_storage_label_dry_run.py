"""Dry-run storage migration into users.storage_label directories.

This script only reads MySQL and scans storage paths. It writes one manifest
under storage/logs/migrations and never moves, copies, or deletes storage data.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine


ROOT = Path(__file__).resolve().parents[2]
STORAGE_ROOT = ROOT / "storage"
PROJECT_ASSETS_ROOT = STORAGE_ROOT / "projects" / "assets"
TRAINING_CORPUS_ROOT = STORAGE_ROOT / "training" / "corpus"
USERS_ROOT = STORAGE_ROOT / "users"
SKIP_ROOTS = [
    STORAGE_ROOT / "temp",
    STORAGE_ROOT / "uploads",
    STORAGE_ROOT / "videos",
]
MANIFEST_ROOT = STORAGE_ROOT / "logs" / "migrations"

LABEL_ALLOWED = re.compile(r"^[A-Za-z0-9@._-]{1,128}$")
LABEL_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9@._-]+")
IMAGE_SAMPLE_NAMES = {"target", "reference", "result"}


@dataclass
class UserInfo:
    id: int
    email: str
    phone: str
    storage_label: str
    proposed_storage_label: str


@dataclass
class DbSnapshot:
    users_by_id: dict[int, UserInfo]
    users_by_identity: dict[str, UserInfo]
    project_owner: dict[int, int]


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _database_url() -> str:
    _load_dotenv()
    url = os.environ.get("COLORCHASE_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("必须配置 COLORCHASE_DATABASE_URL，dry-run 只能读取 MySQL")
    if not url.startswith(("mysql+aiomysql://", "mysql+pymysql://")):
        raise RuntimeError("COLORCHASE_DATABASE_URL 必须使用 mysql+aiomysql:// 或 mysql+pymysql://")
    return url


def build_user_storage_label(identity: str) -> str:
    raw = str(identity or "").strip().lower()
    safe = LABEL_UNSAFE_CHARS.sub("_", raw)
    return ("user_" + safe)[:128]


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _identity_keys(user: UserInfo) -> set[str]:
    keys = {
        str(user.id),
        f"user_{user.id}",
    }
    for value in (user.email, user.phone, user.storage_label, user.proposed_storage_label):
        raw = _clean_text(value)
        if not raw:
            continue
        keys.add(raw)
        keys.add(raw.lower())
    return {key for key in keys if key}


def _build_snapshot(rows: list[dict[str, Any]], project_rows: list[dict[str, Any]]) -> DbSnapshot:
    users_by_id: dict[int, UserInfo] = {}
    users_by_identity: dict[str, UserInfo] = {}
    for row in rows:
        user_id = int(row["id"])
        email = _clean_text(row.get("email"))
        phone = _clean_text(row.get("phone"))
        storage_label = _clean_text(row.get("storage_label"))
        proposed = storage_label
        if not proposed and (email or phone):
            proposed = build_user_storage_label(email or phone)
        user = UserInfo(
            id=user_id,
            email=email,
            phone=phone,
            storage_label=storage_label,
            proposed_storage_label=proposed,
        )
        users_by_id[user_id] = user
        for key in _identity_keys(user):
            users_by_identity[key] = user

    project_owner = {
        int(row["id"]): int(row["owner_id"])
        for row in project_rows
        if row.get("id") is not None and row.get("owner_id") is not None
    }
    return DbSnapshot(users_by_id=users_by_id, users_by_identity=users_by_identity, project_owner=project_owner)


async def _read_db_async(url: str) -> DbSnapshot:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            users = (await conn.execute(text("SELECT id, email, phone, storage_label FROM users"))).mappings().all()
            projects = (await conn.execute(text("SELECT id, owner_id FROM projects"))).mappings().all()
        return _build_snapshot(list(users), list(projects))
    finally:
        await engine.dispose()


def _read_db_sync(url: str) -> DbSnapshot:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            users = conn.execute(text("SELECT id, email, phone, storage_label FROM users")).mappings().all()
            projects = conn.execute(text("SELECT id, owner_id FROM projects")).mappings().all()
        return _build_snapshot(list(users), list(projects))
    finally:
        engine.dispose()


def read_db_snapshot(url: str) -> DbSnapshot:
    if url.startswith("mysql+aiomysql://"):
        return asyncio.run(_read_db_async(url))
    return _read_db_sync(url)


def _is_relative_safe(path: Path) -> bool:
    return not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)


def _inside_storage(path: Path) -> bool:
    try:
        resolved = path.resolve()
        storage = STORAGE_ROOT.resolve()
    except OSError:
        return False
    return resolved == storage or storage in resolved.parents


def _dir_stats(path: Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    if not path.exists():
        return file_count, total_bytes
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            file_count += 1
            total_bytes += item.stat().st_size
        except OSError:
            continue
    return file_count, total_bytes


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_dir(path: Path) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    if not path.exists():
        return result
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(path).as_posix()
        try:
            result[rel] = (item.stat().st_size, _hash_file(item))
        except OSError:
            result[rel] = (-1, "read_error")
    return result


def _compare_existing_destination(source: Path, destination: Path) -> Optional[str]:
    if not destination.exists():
        return None
    if _hash_dir(source) == _hash_dir(destination):
        return "destination_exists_same_hash"
    return "destination_exists_different_hash"


def _valid_label(label: str) -> bool:
    return bool(label and LABEL_ALLOWED.match(label))


def _user_for_label(db: DbSnapshot, user_id: Optional[int]) -> tuple[Optional[UserInfo], list[str]]:
    conflicts: list[str] = []
    if user_id is None:
        conflicts.append("ambiguous_owner")
        return None, conflicts
    user = db.users_by_id.get(int(user_id))
    if user is None:
        conflicts.append("missing_user")
        return None, conflicts
    if not user.storage_label:
        conflicts.append("missing_storage_label")
    label = user.storage_label or user.proposed_storage_label
    if label and not _valid_label(label):
        conflicts.append("unsafe_path")
    return user, conflicts


def _match_user_from_container(db: DbSnapshot, name: str) -> Optional[UserInfo]:
    raw = _clean_text(name)
    if not raw:
        return None
    candidates = [raw, raw.lower()]
    if raw.startswith("user_"):
        candidates.extend([raw[5:], raw[5:].lower()])
    for candidate in candidates:
        user = db.users_by_identity.get(candidate)
        if user:
            return user
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_entry_action(conflicts: list[str], destination_conflict: Optional[str], source: Path, destination: Path) -> tuple[str, list[str], str]:
    all_conflicts = list(dict.fromkeys([*conflicts, *( [destination_conflict] if destination_conflict else [] )]))
    if "unsafe_path" in all_conflicts:
        return "unsafe_path", all_conflicts, "low"
    if destination.resolve() == source.resolve():
        return "skip_by_policy", all_conflicts, "high"
    if destination_conflict == "destination_exists_same_hash":
        return "destination_exists_same_hash", all_conflicts, "high"
    if destination_conflict == "destination_exists_different_hash":
        return "destination_exists_different_hash", all_conflicts, "low"
    blocking = {"missing_project", "missing_user", "missing_storage_label", "ambiguous_owner", "owner_mismatch"}
    if any(item in blocking for item in all_conflicts):
        return "manual_review", all_conflicts, "low"
    return "would_move", all_conflicts, "high"


def _entry(
    *,
    category: str,
    source: Path,
    destination: Path,
    legacy_container: str,
    owner_user_id: Optional[int],
    storage_label: str,
    conflicts: list[str],
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    safe_conflicts = list(conflicts)
    if not _inside_storage(source) or not _inside_storage(destination):
        safe_conflicts.append("unsafe_path")
    dest_conflict = _compare_existing_destination(source, destination) if destination.exists() else None
    action, merged_conflicts, confidence = _resolve_entry_action(safe_conflicts, dest_conflict, source, destination)
    file_count, total_bytes = _dir_stats(source)
    payload = {
        "category": category,
        "action": action,
        "confidence": confidence,
        "source": str(source),
        "destination": str(destination),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "owner_user_id": owner_user_id,
        "storage_label": storage_label,
        "legacy_container": legacy_container,
        "conflicts": merged_conflicts,
    }
    if extra:
        payload.update(extra)
    return payload


def scan_project_assets(db: DbSnapshot) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not PROJECT_ASSETS_ROOT.exists():
        return entries
    for legacy_container in sorted(PROJECT_ASSETS_ROOT.iterdir()):
        if not legacy_container.is_dir():
            continue
        for project_dir in sorted(legacy_container.iterdir()):
            if not project_dir.is_dir():
                continue
            conflicts: list[str] = []
            try:
                project_id = int(project_dir.name)
            except ValueError:
                entries.append(
                    _entry(
                        category="project_assets",
                        source=project_dir,
                        destination=project_dir,
                        legacy_container=legacy_container.name,
                        owner_user_id=None,
                        storage_label="",
                        conflicts=["missing_project"],
                        extra={"project_id": project_dir.name},
                    )
                )
                continue
            owner_id = db.project_owner.get(project_id)
            if owner_id is None:
                conflicts.append("missing_project")
            container_user = _match_user_from_container(db, legacy_container.name)
            if owner_id is not None and container_user and container_user.id != owner_id:
                conflicts.append("owner_mismatch")
            user, user_conflicts = _user_for_label(db, owner_id)
            conflicts.extend(user_conflicts)
            label = user.storage_label or user.proposed_storage_label if user else ""
            destination = PROJECT_ASSETS_ROOT / label / str(project_id) if label else PROJECT_ASSETS_ROOT / "_unresolved" / str(project_id)
            entries.append(
                _entry(
                    category="project_assets",
                    source=project_dir,
                    destination=destination,
                    legacy_container=legacy_container.name,
                    owner_user_id=owner_id,
                    storage_label=label,
                    conflicts=conflicts,
                    extra={"project_id": project_id},
                )
            )
    return entries


def _sample_dirs_under(container: Path) -> list[tuple[Path, str, str]]:
    result: list[tuple[Path, str, str]] = []
    if (container / "meta.json").exists():
        return [(container, container.name, "")]
    for child in sorted(container.iterdir()) if container.exists() else []:
        if not child.is_dir():
            continue
        if (child / "meta.json").exists() or any((child / f"{name}.jpg").exists() for name in IMAGE_SAMPLE_NAMES):
            result.append((child, child.name, ""))
            continue
        for maybe_time in sorted(child.iterdir()):
            if not maybe_time.is_dir():
                continue
            for sample in sorted(maybe_time.iterdir()):
                if sample.is_dir() and ((sample / "meta.json").exists() or any(sample.glob("target.*"))):
                    result.append((sample, sample.name, child.name))
    return result


def _training_owner(db: DbSnapshot, meta: dict[str, Any], project_id: Optional[int], container_name: str) -> tuple[Optional[int], list[str]]:
    conflicts: list[str] = []
    raw_user_id = meta.get("user_id")
    owner_id: Optional[int] = None
    if raw_user_id not in (None, ""):
        try:
            owner_id = int(raw_user_id)
        except (TypeError, ValueError):
            conflicts.append("ambiguous_owner")
    if owner_id is None and project_id is not None:
        owner_id = db.project_owner.get(project_id)
        if owner_id is None:
            conflicts.append("missing_project")
    if owner_id is None:
        matched = _match_user_from_container(db, container_name)
        if matched:
            owner_id = matched.id
    container_user = _match_user_from_container(db, container_name)
    if owner_id is not None and container_user and container_user.id != owner_id:
        conflicts.append("owner_mismatch")
    if owner_id is None:
        conflicts.append("ambiguous_owner")
    return owner_id, conflicts


def scan_training_corpus(db: DbSnapshot) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not TRAINING_CORPUS_ROOT.exists():
        return entries
    for container in sorted(TRAINING_CORPUS_ROOT.iterdir()):
        if not container.is_dir():
            continue
        for sample_dir, sample_id, tier_hint in _sample_dirs_under(container):
            meta = _read_json(sample_dir / "meta.json")
            raw_project_id = meta.get("project_id")
            project_id = None
            if raw_project_id not in (None, ""):
                try:
                    project_id = int(raw_project_id)
                except (TypeError, ValueError):
                    project_id = None
            owner_id, conflicts = _training_owner(db, meta, project_id, container.name)
            user, user_conflicts = _user_for_label(db, owner_id)
            conflicts.extend(user_conflicts)
            label = user.storage_label or user.proposed_storage_label if user else ""
            destination = TRAINING_CORPUS_ROOT / label / sample_id if label else TRAINING_CORPUS_ROOT / "_unresolved" / sample_id
            entries.append(
                _entry(
                    category="training_corpus",
                    source=sample_dir,
                    destination=destination,
                    legacy_container=container.name,
                    owner_user_id=owner_id,
                    storage_label=label,
                    conflicts=conflicts,
                    extra={"project_id": project_id, "sample_id": sample_id, "tier_hint": tier_hint or meta.get("tier") or ""},
                )
            )
    return entries


def _profile_user_ids() -> set[int]:
    store = _read_json(USERS_ROOT / "local_user" / "profiles" / "user_profiles.json")
    result: set[int] = set()
    for key in store.keys():
        try:
            result.add(int(key))
        except (TypeError, ValueError):
            continue
    return result


def scan_user_assets(db: DbSnapshot) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not USERS_ROOT.exists():
        return entries
    profile_ids = _profile_user_ids()
    for container in sorted(USERS_ROOT.iterdir()):
        if not container.is_dir():
            continue
        conflicts: list[str] = []
        owner_id: Optional[int] = None
        if container.name == "local_user":
            if len(profile_ids) == 1:
                owner_id = next(iter(profile_ids))
            else:
                conflicts.append("ambiguous_owner")
        else:
            matched = _match_user_from_container(db, container.name)
            if matched:
                owner_id = matched.id
            else:
                conflicts.append("ambiguous_owner")
        user, user_conflicts = _user_for_label(db, owner_id)
        conflicts.extend(user_conflicts)
        label = user.storage_label or user.proposed_storage_label if user else ""
        destination = USERS_ROOT / label if label else USERS_ROOT / "_unresolved"
        entries.append(
            _entry(
                category="user_assets",
                source=container,
                destination=destination,
                legacy_container=container.name,
                owner_user_id=owner_id,
                storage_label=label,
                conflicts=conflicts,
            )
        )
    return entries


def scan_skipped_roots() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for root in SKIP_ROOTS:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            entries.append(
                _entry(
                    category=root.relative_to(STORAGE_ROOT).as_posix(),
                    source=child,
                    destination=child,
                    legacy_container=child.name,
                    owner_user_id=None,
                    storage_label="",
                    conflicts=[],
                    extra={"action_reason": "skip_by_policy"},
                )
            )
            entries[-1]["action"] = "skip_by_policy"
    return entries


def build_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_action = Counter(entry["action"] for entry in entries)
    by_category = Counter(entry["category"] for entry in entries)
    conflicts = Counter(conflict for entry in entries for conflict in entry.get("conflicts", []))
    safe_entries = [entry for entry in entries if entry["action"] == "would_move" and not entry.get("conflicts")]
    manual_entries = [entry for entry in entries if entry["action"] in {"manual_review", "unsafe_path", "destination_exists_different_hash"}]
    skipped_entries = [entry for entry in entries if entry["action"] == "skip_by_policy"]
    conflict_entries = [entry for entry in entries if entry.get("conflicts")]
    return {
        "entry_count": len(entries),
        "by_action": dict(sorted(by_action.items())),
        "by_category": dict(sorted(by_category.items())),
        "conflicts": dict(sorted(conflicts.items())),
        "safe_to_migrate_count": len(safe_entries),
        "safe_to_migrate_bytes": sum(int(entry.get("total_bytes") or 0) for entry in safe_entries),
        "manual_review_count": len(manual_entries),
        "skipped_count": len(skipped_entries),
        "conflict_count": len(conflict_entries),
        "total_bytes_scanned": sum(int(entry.get("total_bytes") or 0) for entry in entries),
    }


def write_manifest(entries: list[dict[str, Any]]) -> Path:
    MANIFEST_ROOT.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    manifest_path = MANIFEST_ROOT / f"storage_label_dry_run_{now.strftime('%Y%m%d_%H%M%S')}.json"
    payload = {
        "mode": "dry_run",
        "generated_at": now.isoformat(timespec="seconds"),
        "storage_root": str(STORAGE_ROOT),
        "summary": build_summary(entries),
        "entries": entries,
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def main() -> None:
    url = _database_url()
    db = read_db_snapshot(url)
    entries: list[dict[str, Any]] = []
    entries.extend(scan_project_assets(db))
    entries.extend(scan_training_corpus(db))
    entries.extend(scan_user_assets(db))
    entries.extend(scan_skipped_roots())
    manifest_path = write_manifest(entries)
    summary = build_summary(entries)
    print(f"manifest={manifest_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
