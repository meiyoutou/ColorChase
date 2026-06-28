import os
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity
from config import NEURALPRESET_LDC_WEIGHTS

_ldc_model = None
_ldc_weights_candidates = []
_env_root = os.environ.get("COLORCHASE_NEURALPRESET_ROOT", "")
if _env_root and os.path.isdir(_env_root):
    _ldc_weights_candidates.append(
        os.path.join(_env_root, "src", "metric", "content_similiary", "ldc.pth")
    )
_ldc_weights_candidates.append(str(NEURALPRESET_LDC_WEIGHTS))

_ldc_weights_path = None
for p in _ldc_weights_candidates:
    if os.path.exists(p):
        _ldc_weights_path = p
        break

if _ldc_weights_path is None:
    _ldc_weights_path = _ldc_weights_candidates[0]

_device = None


def _get_device():
    global _device
    if _device is None:
        import torch
        _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return _device


def _get_ldc_model():
    global _ldc_model
    if _ldc_model is None:
        if not os.path.exists(_ldc_weights_path):
            return None
        import torch
        from .ldc import LDC
        device = _get_device()
        _ldc_model = LDC()
        _ldc_model.load_state_dict(torch.load(_ldc_weights_path, map_location="cpu"))
        _ldc_model.to(device).eval()
    return _ldc_model


def _calculate_ldc_edge(image_np: np.ndarray) -> np.ndarray:
    import torch
    from torchvision import transforms
    from .ldc import postprocess_edges

    model = _get_ldc_model()
    device = _get_device()

    image = Image.fromarray(image_np)
    h, w = image.size
    h = int(h - h % 32)
    w = int(w - w % 32)

    mean = torch.tensor([103.939, 116.779, 123.68]).to(device).unsqueeze(0).unsqueeze(-1).unsqueeze(-1)
    image = transforms.functional.resize(image, (w, h))
    image = transforms.functional.to_tensor(image)[None, ...].to(device) * 255

    edges = model(image - mean)
    avg_edge = postprocess_edges(edges)
    avg_edge = torch.from_numpy(avg_edge).unsqueeze(0).unsqueeze(0) / 255.0

    return avg_edge[0].permute(1, 2, 0).cpu().numpy()


def calc_content_similarity(result_img: np.ndarray, content_img: np.ndarray) -> float:
    model = _get_ldc_model()
    if model is None:
        return _fallback_content_similarity(result_img, content_img)

    result_rgb = np.asarray(Image.fromarray(result_img).convert("RGB"))
    content_rgb = np.asarray(Image.fromarray(content_img).convert("RGB"))

    result_edge = _calculate_ldc_edge(result_rgb)
    content_edge = _calculate_ldc_edge(content_rgb)

    score = structural_similarity(result_edge, content_edge, channel_axis=-1)
    return float(score)


def _fallback_content_similarity(result_img: np.ndarray, content_img: np.ndarray) -> float:
    import cv2
    r_gray = cv2.cvtColor(result_img, cv2.COLOR_RGB2GRAY)
    c_gray = cv2.cvtColor(content_img, cv2.COLOR_RGB2GRAY)

    r_gray = cv2.resize(r_gray, (min(512, r_gray.shape[1]), min(512, r_gray.shape[0])))
    c_gray = cv2.resize(c_gray, (min(512, c_gray.shape[1]), min(512, c_gray.shape[0])))

    r_edge = cv2.Canny(r_gray, 50, 150).astype(np.float32) / 255.0
    c_edge = cv2.Canny(c_gray, 50, 150).astype(np.float32) / 255.0

    if r_edge.shape != c_edge.shape:
        h = min(r_edge.shape[0], c_edge.shape[0])
        w = min(r_edge.shape[1], c_edge.shape[1])
        r_edge = r_edge[:h, :w]
        c_edge = c_edge[:h, :w]

    score = structural_similarity(r_edge, c_edge)
    return float(score)
