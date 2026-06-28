import numpy as np
import cv2
import torch

from config import get_neuralpreset_weight_status
from ..dncm.model import NeuralPresetPipeline, _img_to_tensor, _tensor_to_img

_model_cache = {}


def _get_pipeline(model_dir=None, device=None):
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    status = get_neuralpreset_weight_status()
    cache_key = f"{status['norm_path']}|{status['style_path']}|{device}"
    if cache_key in _model_cache:
        return _model_cache[cache_key]

    if not status["ready"]:
        searched = ", ".join(status["model_dirs"])
        missing = ", ".join(status["missing"])
        raise FileNotFoundError(
            f"NeuralPreset 权重不完整，缺少 {missing}。已查找目录: {searched}"
        )

    pipeline = NeuralPresetPipeline(encoder_name="simple").to(device).eval()
    norm_state = torch.load(status["norm_path"], map_location=device, weights_only=True)
    style_state = torch.load(status["style_path"], map_location=device, weights_only=True)
    pipeline.norm_stage.load_state_dict(norm_state)
    pipeline.style_stage.load_state_dict(style_state)
    print(f"[NeuralPreset] loaded norm={status['norm_path']} style={status['style_path']}")

    _model_cache[cache_key] = pipeline
    return pipeline


def neural_preset_transfer(target_img: np.ndarray, reference_img: np.ndarray,
                           model_dir: str = None, device=None) -> np.ndarray:
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    pipeline = _get_pipeline(model_dir, device)

    target_tensor = _img_to_tensor(target_img, device)
    reference_tensor = _img_to_tensor(reference_img, device)

    with torch.no_grad():
        preset = pipeline.extract_preset(reference_tensor)
        result_tensor = pipeline.apply_preset(target_tensor, preset)

    result_img = _tensor_to_img(result_tensor)
    return result_img
