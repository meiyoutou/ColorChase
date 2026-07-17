"""Apply training corpus moves planned by storage_label dry-run.

Default mode is --check. It only prints the training_corpus entries that would
move. Use --apply explicitly to move directories, or --rollback to undo a prior
apply manifest. This script never handles project assets, user assets, temp,
uploads, or videos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
STORAGE_ROOT = ROOT / "storage"
TRAINING_CORPUS_ROOT = STORAGE_ROOT / "training" / "corpus"
MANIFEST_ROOT = STORAGE_ROOT / "logs" / "migrations"


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def _resolve_input_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def _is_under(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _safe_training_path(value: str) -> Path:
    path = _resolve_input_path(value)
    if not _is_under(path, TRAINING_CORPUS_ROOT):
        raise ValueError("path is outside storage/training/corpus")
    return path


def _dir_stats(path: Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    if not path.exists():
        return file_count, total_bytes
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        file_count += 1
        total_bytes += item.stat().st_size
    return file_count, total_bytes


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_signature(path: Path) -> dict[str, tuple[int, str]]:
    signature: dict[str, tuple[int, str]] = {}
    if not path.exists():
        return signature
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(path).as_posix()
        signature[rel] = (item.stat().st_size, _hash_file(item))
    return signature


def _same_tree(source: Path, destination: Path) -> bool:
    return _tree_signature(source) == _tree_signature(destination)


def _training_entries(dry_run_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = dry_run_manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("dry-run manifest missing entries list")
    return [
        entry
        for entry in entries
        if entry.get("action") == "would_move" and entry.get("category") == "training_corpus"
    ]


def _operation_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    source = _safe_training_path(str(entry.get("source") or ""))
    destination = _safe_training_path(str(entry.get("destination") or ""))
    return {
        "source": str(source),
        "destination": str(destination),
        "bytes": int(entry.get("total_bytes") or 0),
        "file_count": int(entry.get("file_count") or 0),
        "sample_id": entry.get("sample_id"),
        "owner_user_id": entry.get("owner_user_id"),
        "storage_label": entry.get("storage_label"),
    }


def _build_operations(manifest_path: Path) -> list[dict[str, Any]]:
    dry_run_manifest = _load_json(manifest_path)
    operations: list[dict[str, Any]] = []
    for entry in _training_entries(dry_run_manifest):
        operations.append(_operation_from_entry(entry))
    return operations


def run_check(manifest_path: Path) -> None:
    operations = _build_operations(manifest_path)
    print(f"mode=check")
    print(f"manifest={manifest_path}")
    print(f"training_corpus_would_move={len(operations)}")
    total_bytes = 0
    for index, operation in enumerate(operations, 1):
        source = Path(operation["source"])
        destination = Path(operation["destination"])
        total_bytes += int(operation["bytes"])
        exists = source.exists()
        destination_exists = destination.exists()
        print(
            f"{index:02d}. source={_display(source)} -> destination={_display(destination)} "
            f"files={operation['file_count']} bytes={operation['bytes']} "
            f"source_exists={exists} destination_exists={destination_exists}"
        )
    print(f"total_bytes={total_bytes}")


def _apply_one(operation: dict[str, Any]) -> dict[str, Any]:
    source = Path(operation["source"])
    destination = Path(operation["destination"])
    record = {
        "source": str(source),
        "destination": str(destination),
        "status": "",
        "bytes": operation["bytes"],
        "file_count": operation["file_count"],
        "error": "",
    }

    try:
        if not _is_under(source, TRAINING_CORPUS_ROOT) or not _is_under(destination, TRAINING_CORPUS_ROOT):
            record["status"] = "unsafe_path"
            record["error"] = "source or destination is outside storage/training/corpus"
            return record
        if not source.exists():
            record["status"] = "source_missing"
            record["error"] = "source does not exist"
            return record
        if not source.is_dir():
            record["status"] = "source_not_directory"
            record["error"] = "source is not a directory"
            return record

        actual_file_count, actual_bytes = _dir_stats(source)
        record["file_count"] = actual_file_count
        record["bytes"] = actual_bytes

        if destination.exists():
            if _same_tree(source, destination):
                record["status"] = "already_exists_same_hash"
                return record
            record["status"] = "conflict_destination_exists_different_hash"
            record["error"] = "destination exists with different tree hash"
            return record

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        record["status"] = "moved"
        return record
    except Exception as exc:
        record["status"] = "error"
        record["error"] = str(exc)
        return record


def run_apply(manifest_path: Path) -> Path:
    operations = _build_operations(manifest_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    apply_path = MANIFEST_ROOT / f"storage_label_apply_{timestamp}.json"
    records = []
    for operation in operations:
        records.append(_apply_one(operation))

    summary: dict[str, Any] = {
        "dry_run_manifest": str(manifest_path),
        "entry_count": len(records),
        "by_status": {},
        "moved_count": 0,
        "moved_bytes": 0,
    }
    for record in records:
        status = record["status"]
        summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
        if status == "moved":
            summary["moved_count"] += 1
            summary["moved_bytes"] += int(record.get("bytes") or 0)

    _write_json(
        apply_path,
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "apply",
            "summary": summary,
            "entries": records,
        },
    )
    print(f"mode=apply")
    print(f"apply_manifest={apply_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return apply_path


def _rollback_one(record: dict[str, Any]) -> dict[str, Any]:
    destination = _safe_training_path(str(record.get("destination") or ""))
    source = _safe_training_path(str(record.get("source") or ""))
    result = {
        "source": str(source),
        "destination": str(destination),
        "status": "",
        "bytes": int(record.get("bytes") or 0),
        "file_count": int(record.get("file_count") or 0),
        "error": "",
    }

    try:
        if not destination.exists():
            result["status"] = "rollback_destination_missing"
            result["error"] = "moved destination does not exist"
            return result
        if not destination.is_dir():
            result["status"] = "rollback_destination_not_directory"
            result["error"] = "moved destination is not a directory"
            return result
        if source.exists():
            if _same_tree(destination, source):
                result["status"] = "rollback_source_already_exists_same_hash"
                return result
            result["status"] = "rollback_conflict_source_exists"
            result["error"] = "original source exists with different tree hash"
            return result

        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(destination), str(source))
        result["status"] = "rolled_back"
        return result
    except Exception as exc:
        result["status"] = "rollback_error"
        result["error"] = str(exc)
        return result


def run_rollback(apply_manifest_path: Path) -> Path:
    apply_manifest = _load_json(apply_manifest_path)
    moved_records = [
        record
        for record in apply_manifest.get("entries", [])
        if record.get("status") == "moved"
    ]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rollback_path = MANIFEST_ROOT / f"storage_label_rollback_{timestamp}.json"
    records = [_rollback_one(record) for record in moved_records]

    summary: dict[str, Any] = {
        "apply_manifest": str(apply_manifest_path),
        "entry_count": len(records),
        "by_status": {},
        "rolled_back_count": 0,
    }
    for record in records:
        status = record["status"]
        summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
        if status == "rolled_back":
            summary["rolled_back_count"] += 1

    _write_json(
        rollback_path,
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "rollback",
            "summary": summary,
            "entries": records,
        },
    )
    print(f"mode=rollback")
    print(f"rollback_manifest={rollback_path}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return rollback_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply or rollback training corpus storage_label moves.")
    parser.add_argument(
        "manifest",
        nargs="?",
        default="storage/logs/migrations/storage_label_dry_run_20260710_125920.json",
        help="dry-run manifest for --check/--apply",
    )
    parser.add_argument(
        "--manifest",
        dest="manifest_option",
        help="dry-run manifest for --check/--apply; same as the positional manifest",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="print planned training_corpus moves; this is the default")
    mode.add_argument("--apply", action="store_true", help="move training_corpus directories and write apply manifest")
    mode.add_argument("--rollback", help="rollback status=moved entries from an apply manifest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.rollback:
        run_rollback(_resolve_input_path(args.rollback))
        return

    manifest_path = _resolve_input_path(args.manifest_option or args.manifest)
    if args.apply:
        run_apply(manifest_path)
        return

    run_check(manifest_path)


if __name__ == "__main__":
    main()
