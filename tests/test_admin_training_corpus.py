import json

from app.routes.admin import _scan_training_corpus


def test_scan_training_corpus_counts_files_once(tmp_path):
    sample_dir = tmp_path / "user_1" / "sample_1"
    sample_dir.mkdir(parents=True)

    (sample_dir / "target.jpg").write_bytes(b"target")
    (sample_dir / "reference.jpg").write_bytes(b"reference")
    (sample_dir / "result.jpg").write_bytes(b"result")
    (sample_dir / "meta.json").write_text(
        json.dumps(
            {
                "rating": 5,
                "files": {
                    "reference": "reference.jpg",
                    "result": "result.jpg",
                },
            }
        ),
        encoding="utf-8",
    )

    stats = _scan_training_corpus(tmp_path)

    assert stats["user_count"] == 1
    assert stats["sample_count"] == 1
    assert stats["target_count"] == 1
    assert stats["reference_count"] == 1
    assert stats["result_count"] == 1
    assert stats["meta_count"] == 1
    assert stats["rating_count"] == 1
