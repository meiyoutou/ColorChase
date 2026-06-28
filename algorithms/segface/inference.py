import os
import numpy as np
import cv2
import torch
import torch.nn.functional as F

from .model import SegFaceCeleb

WEIGHTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "weights", "segface")
WEIGHTS_FILENAME = "swinb_celeba_512_model_299.pt"
HF_REPO_ID = "kartiknarayan/SegFace"
HF_FILENAME = "swinb_celeba_512/model_299.pt"

_SKIN_INDICES = [1, 2, 4, 5, 6, 7, 10, 11]
_LIP_INDICES = [12, 13]
_HAIR_INDICES = [14]

_IMAGENET_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
_IMAGENET_STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)

_model_cache = {}


def _get_weights_path():
    weights_path = os.path.join(WEIGHTS_DIR, WEIGHTS_FILENAME)
    if os.path.exists(weights_path):
        return weights_path
    os.makedirs(WEIGHTS_DIR, exist_ok=True)

    mirror_url = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
    print(f"[SegFace] Downloading weights from HuggingFace mirror: {mirror_url}")

    import urllib.request
    download_url = f"{mirror_url}/{HF_REPO_ID}/resolve/main/{HF_FILENAME}"
    print(f"[SegFace] URL: {download_url}")
    urllib.request.urlretrieve(download_url, weights_path)

    if os.path.exists(weights_path):
        return weights_path
    raise FileNotFoundError(f"SegFace weights not found after download. Expected at {weights_path}")


def _load_model(device="cuda"):
    cache_key = device
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    actual_device = device
    if actual_device == "cuda" and not torch.cuda.is_available():
        print("[SegFace] CUDA not available, falling back to CPU")
        actual_device = "cpu"

    model = SegFaceCeleb(input_resolution=512, backbone_name="swin_base")
    weights_path = _get_weights_path()
    print(f"[SegFace] Loading weights from: {weights_path}")
    checkpoint = torch.load(weights_path, map_location="cpu", weights_only=False)
    model.load_state_dict(checkpoint['state_dict_backbone'])
    model = model.to(actual_device)
    model.eval()
    _model_cache[cache_key] = model
    return model


def _preprocess(img_bgr):
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    h, w = img_rgb.shape[:2]
    img_resized = cv2.resize(img_rgb, (512, 512), interpolation=cv2.INTER_LINEAR)
    img_float = img_resized.astype(np.float32) / 255.0
    img_tensor = torch.from_numpy(img_float).permute(2, 0, 1).float()
    img_tensor = (img_tensor - _IMAGENET_MEAN) / _IMAGENET_STD
    img_tensor = img_tensor.unsqueeze(0)
    return img_tensor, h, w


def _postprocess(seg_output, orig_h, orig_w):
    probs = F.interpolate(seg_output, size=(orig_h, orig_w), mode='bilinear', align_corners=False)
    probs = probs.softmax(dim=1)
    probs_np = probs[0].detach().cpu().numpy()
    return probs_np


def _extract_masks(probs_np):
    skin = np.zeros(probs_np.shape[1:], dtype=np.float32)
    for idx in _SKIN_INDICES:
        skin += probs_np[idx]
    skin = np.clip(skin, 0.0, 1.0)

    lip = np.zeros(probs_np.shape[1:], dtype=np.float32)
    for idx in _LIP_INDICES:
        lip += probs_np[idx]
    lip = np.clip(lip, 0.0, 1.0)

    hair = probs_np[_HAIR_INDICES[0]].astype(np.float32)
    hair = np.clip(hair, 0.0, 1.0)

    return skin, lip, hair


def parse_face_semantics(img_bgr: np.ndarray, device: str = "cuda") -> dict:
    if img_bgr.dtype != np.uint8:
        raise ValueError(f"Input must be uint8, got {img_bgr.dtype}")
    if len(img_bgr.shape) != 3 or img_bgr.shape[2] != 3:
        raise ValueError(f"Input must be (H,W,3) BGR image, got shape {img_bgr.shape}")

    model = _load_model(device)
    actual_device = next(model.parameters()).device

    img_tensor, orig_h, orig_w = _preprocess(img_bgr)
    img_tensor = img_tensor.to(actual_device)

    with torch.no_grad():
        seg_output = model(img_tensor)

    probs_np = _postprocess(seg_output, orig_h, orig_w)
    skin, lip, hair = _extract_masks(probs_np)

    return {
        'skin': skin,
        'lip': lip,
        'hair': hair,
    }


if __name__ == '__main__':
    import sys
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    debug_dir = os.path.join(project_root, "debug_output")
    os.makedirs(debug_dir, exist_ok=True)

    test_img_path = None
    for candidate in [
        os.path.join(debug_dir, "0_reference.jpg"),
        os.path.join(debug_dir, "1_modflows_raw.jpg"),
        os.path.join(debug_dir, "2_final.jpg"),
    ]:
        if os.path.exists(candidate):
            test_img_path = candidate
            break

    if test_img_path is None:
        print("[SegFace Test] No test image found in debug_output, generating random image")
        test_img = np.random.randint(0, 256, (512, 512, 3), dtype=np.uint8)
        test_img_path = "random_512x512"
    else:
        raw = np.fromfile(test_img_path, dtype=np.uint8)
        test_img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if test_img is None:
            test_img = cv2.imread(test_img_path)
        print(f"[SegFace Test] Loaded test image from: {test_img_path}")

    print(f"[SegFace Test] Input shape: {test_img.shape}, dtype: {test_img.dtype}")

    result = parse_face_semantics(test_img, device="cuda")

    for name in ['skin', 'lip', 'hair']:
        mask = result[name]
        out_path = os.path.join(debug_dir, f"segface_test_{name}.png")
        mask_uint8 = (mask * 255).astype(np.uint8)
        cv2.imencode('.png', mask_uint8)[1].tofile(out_path)
        print(f"[SegFace Test] {name}: shape={mask.shape}, dtype={mask.dtype}, min={mask.min():.4f}, max={mask.max():.4f}")
        print(f"[SegFace Test] Saved: {out_path}")

    print("\n========== SegFace Verification Report ==========")
    weights_path = os.path.join(WEIGHTS_DIR, WEIGHTS_FILENAME)
    weights_exist = os.path.exists(weights_path)
    print(f"Model loaded: {'YES' if weights_exist else 'NO (downloaded)'}")
    print(f"Weights path: {weights_path}")
    print(f"Input image shape: {test_img.shape}")
    for name in ['skin', 'lip', 'hair']:
        mask = result[name]
        print(f"Output '{name}': shape={mask.shape}, dtype={mask.dtype}, min={mask.min():.4f}, max={mask.max():.4f}")
    print(f"Test images saved to: {debug_dir}")
    print("=================================================")
