import os
import numpy as np
from PIL import Image
from config import NEURALPRESET_STYLE_ONNX

_style_sess = None
_style_onnx_candidates = []
_env_root = os.environ.get("COLORCHASE_NEURALPRESET_ROOT", "")
if _env_root and os.path.isdir(_env_root):
    _style_onnx_candidates.append(
        os.path.join(_env_root, "src", "metric", "style_similiary", "StyleSimiliaryDiscriminator.onnx")
    )
_style_onnx_candidates.append(str(NEURALPRESET_STYLE_ONNX))

_style_onnx_path = None
for p in _style_onnx_candidates:
    if os.path.exists(p):
        _style_onnx_path = p
        break

if _style_onnx_path is None:
    _style_onnx_path = _style_onnx_candidates[0]


def _get_style_session():
    global _style_sess
    if _style_sess is None:
        if not os.path.exists(_style_onnx_path):
            return None
        import onnxruntime
        _style_sess = onnxruntime.InferenceSession(_style_onnx_path)
    return _style_sess


def calc_style_similarity(result_img: np.ndarray, style_img: np.ndarray) -> float:
    sess = _get_style_session()
    if sess is None:
        return _fallback_style_similarity(result_img, style_img)

    result_resized = np.asarray(
        Image.fromarray(result_img).convert("RGB").resize((512, 512))
    ).astype(np.float32)
    style_resized = np.asarray(
        Image.fromarray(style_img).convert("RGB").resize((512, 512))
    ).astype(np.float32)

    score = sess.run(["score"], {"ref": style_resized, "img": result_resized})[0]
    return float(score)


def _fallback_style_similarity(result_img: np.ndarray, style_img: np.ndarray) -> float:
    import cv2
    r_lab = cv2.cvtColor(result_img, cv2.COLOR_RGB2LAB).astype(np.float64)
    s_lab = cv2.cvtColor(style_img, cv2.COLOR_RGB2LAB).astype(np.float64)

    r_mean = r_lab.reshape(-1, 3).mean(axis=0)
    s_mean = s_lab.reshape(-1, 3).mean(axis=0)
    r_std = r_lab.reshape(-1, 3).std(axis=0)
    s_std = s_lab.reshape(-1, 3).std(axis=0)

    mean_dist = np.sqrt(np.sum((r_mean - s_mean) ** 2))
    std_dist = np.sqrt(np.sum((r_std - s_std) ** 2))

    score = max(0.0, 1.0 - (mean_dist / 300.0 + std_dist / 100.0) / 2.0)
    return float(score)
