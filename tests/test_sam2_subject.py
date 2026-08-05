from pathlib import Path

import numpy as np

import algorithms.sam2_subject as sam2_subject
import algorithms.subject_mask as subject_mask


def test_find_weight_prefers_small(tmp_path):
    weights_dir = tmp_path / "weights" / "sam2"
    weights_dir.mkdir(parents=True)
    (weights_dir / "sam2_hiera_base_plus.pt").write_bytes(b"base")
    (weights_dir / "sam2_hiera_small.pt").write_bytes(b"small")

    selected = sam2_subject._find_weight(tmp_path)

    assert selected is not None
    assert selected[0].name == "sam2_hiera_small.pt"
    assert selected[1] == "sam2_hiera_s"
    assert selected[2] == "sam2_small"


def test_sam2_status_requires_runtime_and_weight(tmp_path, monkeypatch):
    weights_dir = tmp_path / "weights" / "sam2"
    weights_dir.mkdir(parents=True)
    (weights_dir / "sam2_hiera_small.pt").write_bytes(b"small")

    monkeypatch.setattr(sam2_subject, "_sam2_runtime_ready", lambda: False)
    status = sam2_subject.get_sam2_status(tmp_path)

    assert status["selected_model"] == "sam2_small"
    assert status["selected_path"].endswith("sam2_hiera_small.pt")
    assert status["ready"] is False


def test_generate_subject_mask_uses_sam2(monkeypatch):
    image = np.zeros((16, 20, 3), dtype=np.uint8)
    expected_mask = np.zeros((16, 20), dtype=np.float32)
    expected_mask[2:14, 5:15] = 1.0

    monkeypatch.setattr(
        subject_mask,
        "predict_sam2_subject_mask",
        lambda *args, **kwargs: (
            expected_mask,
            {"source": "sam2", "model_choice": "sam2_small"},
        ),
    )
    monkeypatch.setattr(subject_mask, "_refine_mask", lambda image, mask: mask)

    mask, meta = subject_mask.generate_subject_mask(
        image,
        model_choice="sam2",
        prefer_birefnet=False,
    )

    assert np.array_equal(mask, expected_mask)
    assert meta["source"] == "sam2"


def test_generate_subject_mask_falls_back_after_sam2_error(monkeypatch):
    image = np.zeros((16, 20, 3), dtype=np.uint8)
    fallback_mask = np.zeros((16, 20), dtype=np.float32)
    fallback_mask[3:13, 6:14] = 1.0

    def fail_sam2(*args, **kwargs):
        raise RuntimeError("test SAM2 failure")

    monkeypatch.setattr(subject_mask, "predict_sam2_subject_mask", fail_sam2)
    monkeypatch.setattr(
        subject_mask,
        "_person_or_subject_mask",
        lambda image: np.zeros(image.shape[:2], dtype=np.float32),
    )
    monkeypatch.setattr(
        subject_mask,
        "_grabcut_center_subject",
        lambda image: fallback_mask,
    )
    monkeypatch.setattr(subject_mask, "_refine_mask", lambda image, mask: mask)

    mask, meta = subject_mask.generate_subject_mask(
        image,
        model_choice="sam2",
        prefer_birefnet=False,
    )

    assert np.array_equal(mask, fallback_mask)
    assert meta["source"] == "grabcut_center"
    assert meta["fallback_from"] == "sam2"
    assert "test SAM2 failure" in meta["sam2_error"]
