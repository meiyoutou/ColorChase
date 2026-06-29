import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import cv2
from config import get_neuralpreset_weight_status


class ColorMappingNetwork(nn.Module):
    def __init__(self, in_channels=3, mid_channels=64, num_bottlenecks=4):
        super(ColorMappingNetwork, self).__init__()
        self.encoder = nn.Sequential(
            nn.Conv2d(in_channels, mid_channels, 1, 1, 0),
            nn.ReLU(inplace=True),
            nn.Conv2d(mid_channels, mid_channels, 1, 1, 0),
            nn.ReLU(inplace=True),
        )
        bottleneck = []
        for _ in range(num_bottlenecks):
            bottleneck.append(nn.Conv2d(mid_channels, mid_channels, 1, 1, 0))
            bottleneck.append(nn.ReLU(inplace=True))
        self.bottleneck = nn.Sequential(*bottleneck)
        self.decoder = nn.Sequential(
            nn.Conv2d(mid_channels, mid_channels, 1, 1, 0),
            nn.ReLU(inplace=True),
            nn.Conv2d(mid_channels, 9, 1, 1, 0),
        )

    def forward(self, x):
        feat = self.encoder(x)
        feat = self.bottleneck(feat)
        params = self.decoder(feat)
        B, _, H, W = params.shape
        mapping_matrix = params.view(B, 3, 3, H, W)
        mapped = torch.einsum('bcihw,bijhw->bcjhw', x, mapping_matrix)
        return mapped


class DNCM(nn.Module):
    def __init__(self, encoder_name='efficientnet_b0', pretrained=True):
        super(DNCM, self).__init__()
        self.feature_extractor = _build_encoder(encoder_name, pretrained)
        feat_dim = self.feature_extractor.feat_dim
        self.mapping_network = ColorMappingNetwork(in_channels=3, mid_channels=64)
        self.param_predictor = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Flatten(),
            nn.Linear(feat_dim, 256),
            nn.ReLU(inplace=True),
            nn.Linear(256, 9),
        )

    def forward(self, img, return_mapping=False):
        features = self.feature_extractor(img)
        mapping_params = self.param_predictor(features)
        B = img.shape[0]
        mapping_matrix = mapping_params.view(B, 3, 3)

        img_flat = img.view(B, 3, -1)
        mapped = torch.bmm(mapping_matrix, img_flat)
        mapped = mapped.view(B, 3, img.shape[2], img.shape[3])

        if return_mapping:
            return mapped, mapping_matrix
        return mapped


class _SimpleEncoder(nn.Module):
    def __init__(self, feat_dim=128):
        super(_SimpleEncoder, self).__init__()
        self.feat_dim = feat_dim
        self.conv = nn.Sequential(
            nn.Conv2d(3, 32, 3, 2, 1), nn.ReLU(inplace=True),
            nn.Conv2d(32, 64, 3, 2, 1), nn.ReLU(inplace=True),
            nn.Conv2d(64, 128, 3, 2, 1), nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d(1),
        )

    def forward(self, x):
        return self.conv(x)


def _build_encoder(name, pretrained):
    if name == 'simple':
        encoder = _SimpleEncoder(feat_dim=128)
    else:
        try:
            from torchvision.models import efficientnet_b0, EfficientNet_B0_Weights
            backbone = efficientnet_b0(weights=EfficientNet_B0_Weights.DEFAULT if pretrained else None)
            encoder = _EffNetEncoder(backbone)
        except Exception:
            encoder = _SimpleEncoder(feat_dim=128)
    return encoder


class _EffNetEncoder(nn.Module):
    def __init__(self, backbone):
        super(_EffNetEncoder, self).__init__()
        self.features = backbone.features
        self.feat_dim = backbone.features[-1].out_channels

    def forward(self, x):
        return self.features(x)


class NormalizationStage(nn.Module):
    def __init__(self, encoder_name='simple'):
        super(NormalizationStage, self).__init__()
        self.dncm = DNCM(encoder_name=encoder_name, pretrained=False)

    def forward(self, img, return_mapping=False):
        return self.dncm(img, return_mapping=return_mapping)


class StylizationStage(nn.Module):
    def __init__(self, encoder_name='simple'):
        super(StylizationStage, self).__init__()
        self.dncm = DNCM(encoder_name=encoder_name, pretrained=False)

    def forward(self, normalized_img, return_mapping=False):
        return self.dncm(normalized_img, return_mapping=return_mapping)


class NeuralPresetPipeline(nn.Module):
    def __init__(self, encoder_name='simple'):
        super(NeuralPresetPipeline, self).__init__()
        self.norm_stage = NormalizationStage(encoder_name)
        self.style_stage = StylizationStage(encoder_name)

    def forward(self, img, return_mapping=False):
        normalized, norm_mapping = self.norm_stage(img, return_mapping=True)
        stylized, style_mapping = self.style_stage(normalized, return_mapping=True)
        if return_mapping:
            return stylized, norm_mapping, style_mapping
        return stylized

    def extract_preset(self, style_img):
        with torch.no_grad():
            normalized, _ = self.norm_stage(style_img, return_mapping=True)
            _, style_mapping = self.style_stage(normalized, return_mapping=True)
        return style_mapping

    def apply_preset(self, content_img, preset_mapping):
        with torch.no_grad():
            normalized, _ = self.norm_stage(content_img, return_mapping=True)
            B, _, H, W = normalized.shape
            img_flat = normalized.view(B, 3, -1)
            stylized = torch.bmm(preset_mapping, img_flat)
            stylized = stylized.view(B, 3, H, W)
        return stylized


def dncm_transfer(target_img: np.ndarray, reference_img: np.ndarray,
                  model_path: str = None, device=None) -> np.ndarray:
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    pipeline = NeuralPresetPipeline(encoder_name='simple')

    if model_path and os.path.exists(model_path):
        state_dict = torch.load(model_path, map_location=device)
        pipeline.load_state_dict(state_dict)

    pipeline.to(device).eval()

    target_tensor = _img_to_tensor(target_img, device)
    reference_tensor = _img_to_tensor(reference_img, device)

    with torch.no_grad():
        preset = pipeline.extract_preset(reference_tensor)
        result_tensor = pipeline.apply_preset(target_tensor, preset)

    result_img = _tensor_to_img(result_tensor)
    return result_img


def _img_to_tensor(img: np.ndarray, device) -> torch.Tensor:
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    rgb = rgb.astype(np.float32) / 255.0
    tensor = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(device)
    return tensor


def _tensor_to_img(tensor: torch.Tensor) -> np.ndarray:
    arr = tensor.squeeze(0).permute(1, 2, 0).cpu().numpy()
    arr = np.clip(arr * 255, 0, 255).astype(np.uint8)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _check_dncm_weights(model_dir: str) -> tuple:
    status = get_neuralpreset_weight_status()
    return status["ready"], status["norm_path"], status["style_path"]


def generate_lut_from_dncm(reference_img: np.ndarray, target_img: np.ndarray,
                            lut_size: int = 33, model_dir: str = None,
                            device=None) -> np.ndarray:
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    has_weights, norm_path, style_path = _check_dncm_weights(model_dir)
    if not has_weights:
        status = get_neuralpreset_weight_status()
        searched = ", ".join(status["model_dirs"])
        missing = ", ".join(status["missing"])
        raise FileNotFoundError(
            f"DNCM / NeuralPreset LUT 权重不完整，缺少 {missing}。已查找目录: {searched}"
        )

    pipeline = NeuralPresetPipeline(encoder_name='simple').to(device).eval()

    pipeline.norm_stage.load_state_dict(
        torch.load(norm_path, map_location=device, weights_only=True))
    pipeline.style_stage.load_state_dict(
        torch.load(style_path, map_location=device, weights_only=True))

    target_tensor = _img_to_tensor(target_img, device)
    reference_tensor = _img_to_tensor(reference_img, device)

    with torch.no_grad():
        _, norm_matrix = pipeline.norm_stage(target_tensor, return_mapping=True)
        style_mapping = pipeline.extract_preset(reference_tensor)

    grid_1d = np.linspace(0, 1, lut_size, dtype=np.float32)
    r_grid, g_grid, b_grid = np.meshgrid(grid_1d, grid_1d, grid_1d, indexing='ij')
    grid_points = np.stack([r_grid, g_grid, b_grid], axis=-1).reshape(-1, 3)
    grid_tensor = torch.from_numpy(grid_points).to(device)

    norm_matrix_np = norm_matrix.cpu().numpy()
    style_matrix_np = style_mapping.cpu().numpy()

    norm_grid = grid_tensor.cpu().numpy() @ norm_matrix_np[0].T
    norm_grid = np.clip(norm_grid, 0, 1)

    mapped_grid = norm_grid @ style_matrix_np[0].T
    mapped_grid = np.clip(mapped_grid, 0, 1)

    lut = mapped_grid.reshape(lut_size, lut_size, lut_size, 3).astype(np.float32)
    return lut
