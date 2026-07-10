import asyncio
import json
from io import BytesIO

import local_storage_api


class FakeUpload:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


def test_training_upload_uses_storage_label_dir(monkeypatch, tmp_path):
    training_root = tmp_path / "storage" / "training" / "corpus"
    monkeypatch.setattr(
        local_storage_api,
        "_training_corpus_dir_for_label",
        lambda storage_label: training_root / storage_label,
    )
    monkeypatch.setattr(local_storage_api, "_is_admin_request", lambda authorization: True)
    monkeypatch.setattr(local_storage_api, "_get_request_user_id", lambda authorization: 7)
    monkeypatch.setattr(local_storage_api, "_get_user_email", lambda authorization: asyncio.sleep(0, result="admin@example.com"))
    monkeypatch.setattr(local_storage_api, "resolve_user_storage_label", lambda user_id: asyncio.sleep(0, result="user_admin@example.com"))

    response = asyncio.run(
        local_storage_api.api_training_upload(
            storage_check=None,
            target=FakeUpload("target.jpg", b"target"),
            reference=FakeUpload("reference.jpg", b"reference"),
            result=FakeUpload("result.jpg", b"result"),
            meta=json.dumps({"rating": 5}),
            sample_uuid="sample_1",
            is_video="0",
            authorization="Bearer token",
        )
    )

    payload = json.loads(response.body)
    sample_dir = training_root / "user_admin@example.com" / "sample_1"

    assert payload["ok"] is True
    assert payload["tier"] == "high_rating"
    assert payload["path"] == str(sample_dir)
    assert (sample_dir / "target.jpg").read_bytes() == b"target"
    assert (sample_dir / "reference.jpg").read_bytes() == b"reference"
    assert (sample_dir / "result.jpg").read_bytes() == b"result"
    assert not (training_root / "high_rating").exists()

    meta = json.loads((sample_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["storage_label"] == "user_admin@example.com"
    assert meta["user_folder"] == "user_admin@example.com"
    assert meta["tier"] == "high_rating"
