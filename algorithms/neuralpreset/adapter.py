import torch, copy, collections, re
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import cv2
import os


class SqueezeExcitation(nn.Module):
    def __init__(self, in_ch, reduced_ch):
        super().__init__()
        self.se_reduce = nn.Conv2d(in_ch, reduced_ch, 1)
        self.se_expand = nn.Conv2d(reduced_ch, in_ch, 1)

    def forward(self, x):
        s = F.adaptive_avg_pool2d(x, 1)
        s = self.se_reduce(s)
        s = F.silu(s)
        s = self.se_expand(s)
        s = torch.sigmoid(s)
        return x * s


class MBConvBlock(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self._has_expand = cfg.get('has_expand', True)
        if self._has_expand:
            self._expand_conv = nn.Conv2d(cfg['in_ch'], cfg['exp_ch'], 1, bias=False)
            self._bn0 = nn.BatchNorm2d(cfg['exp_ch'])
        self._depthwise_conv = nn.Conv2d(
            cfg['exp_ch'], cfg['exp_ch'],
            cfg['kernel'], padding=cfg['kernel'] // 2,
            groups=cfg['exp_ch'], bias=False
        )
        self._bn1 = nn.BatchNorm2d(cfg['exp_ch'])
        se_red = int(cfg['exp_ch'] * cfg['se_ratio']) if 'se_ratio' in cfg else max(1, cfg['exp_ch'] // cfg['se_divisor'])
        self._se_reduce = nn.Conv2d(cfg['exp_ch'], se_red, 1)
        self._se_expand = nn.Conv2d(se_red, cfg['exp_ch'], 1)
        self._project_conv = nn.Conv2d(cfg['exp_ch'], cfg['out_ch'], 1, bias=False)
        self._bn2 = nn.BatchNorm2d(cfg['out_ch'])
        self._has_residual = cfg['stride'] == 1 and cfg['in_ch'] == cfg['out_ch']

    def forward(self, x):
        residual = x
        if self._has_expand:
            x = self._expand_conv(x)
            x = self._bn0(x)
            x = F.silu(x)
        x = self._depthwise_conv(x)
        x = self._bn1(x)
        x = F.silu(x)
        x = x * torch.sigmoid(self._se_expand(F.silu(self._se_reduce(
            F.adaptive_avg_pool2d(x, 1)))))
        x = self._project_conv(x)
        x = self._bn2(x)
        if self._has_residual:
            x = x + residual
        return x


class EfficientNetB0Encoder(nn.Module):
    def __init__(self):
        super().__init__()
        self._conv_stem = nn.Conv2d(3, 32, 3, stride=2, padding=1, bias=False)
        self._bn0 = nn.BatchNorm2d(32)

        block_configs = [
            {'in_ch': 32, 'exp_ch': 32, 'out_ch': 16, 'kernel': 3, 'stride': 1, 'has_expand': False, 'se_ratio': 0.25},
            {'in_ch': 16, 'exp_ch': 96, 'out_ch': 24, 'kernel': 3, 'stride': 2, 'se_divisor': 24},
            {'in_ch': 24, 'exp_ch': 144, 'out_ch': 24, 'kernel': 3, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 24, 'exp_ch': 144, 'out_ch': 40, 'kernel': 5, 'stride': 2, 'se_divisor': 24},
            {'in_ch': 40, 'exp_ch': 240, 'out_ch': 40, 'kernel': 5, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 40, 'exp_ch': 240, 'out_ch': 80, 'kernel': 3, 'stride': 2, 'se_divisor': 24},
            {'in_ch': 80, 'exp_ch': 480, 'out_ch': 80, 'kernel': 3, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 80, 'exp_ch': 480, 'out_ch': 80, 'kernel': 3, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 80, 'exp_ch': 480, 'out_ch': 112, 'kernel': 5, 'stride': 2, 'se_divisor': 24},
            {'in_ch': 112, 'exp_ch': 672, 'out_ch': 112, 'kernel': 5, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 112, 'exp_ch': 672, 'out_ch': 112, 'kernel': 5, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 112, 'exp_ch': 672, 'out_ch': 192, 'kernel': 5, 'stride': 2, 'se_divisor': 24},
            {'in_ch': 192, 'exp_ch': 1152, 'out_ch': 192, 'kernel': 5, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 192, 'exp_ch': 1152, 'out_ch': 192, 'kernel': 5, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 192, 'exp_ch': 1152, 'out_ch': 192, 'kernel': 5, 'stride': 1, 'se_divisor': 24},
            {'in_ch': 192, 'exp_ch': 1152, 'out_ch': 320, 'kernel': 3, 'stride': 1, 'se_divisor': 24},
        ]

        self._blocks = nn.ModuleList([MBConvBlock(cfg) for cfg in block_configs])
        self._conv_head = nn.Conv2d(320, 1280, 1, bias=False)
        self._bn1 = nn.BatchNorm2d(1280)
        self._fc = nn.Linear(1280, 512)

    def forward(self, x):
        x = self._conv_stem(x)
        x = self._bn0(x)
        x = F.silu(x)
        for block in self._blocks:
            x = block(x)
        x = self._conv_head(x)
        x = self._bn1(x)
        x = F.silu(x)
        x = F.adaptive_avg_pool2d(x, 1)
        x = x.flatten(1)
        x = self._fc(x)
        return x


class NeuralPresetStyler(nn.Module):
    def __init__(self, k=16):
        super().__init__()
        self.style_encoder = EfficientNetB0Encoder()
        self.transform_p = nn.Parameter(torch.randn(3, k) * 0.02)
        self.transform_q = nn.Parameter(torch.randn(k, 3) * 0.02)
        self.k = k

    def forward_style(self, ref_img):
        B = ref_img.shape[0]
        ref_feat = self.style_encoder(ref_img)
        style_feats = ref_feat.view(B, self.k, -1).mean(dim=2)
        return style_feats

    def apply_style(self, target_img, style_feats):
        B, C, H, W = target_img.shape
        diag = torch.diag_embed(style_feats)
        eff_3x3 = torch.einsum('ij,bjk,kl->bil', self.transform_q.T, diag, self.transform_p.T)
        img_flat = target_img.view(B, C, -1)
        out_flat = torch.bmm(eff_3x3, img_flat)
        out = out_flat.view(B, C, H, W)
        return torch.clamp(out, 0, 1)


_model_cache = None


def _img_to_tensor(img_bgr, device):
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    rgb = rgb.astype(np.float32) / 255.0 * 2.0 - 1.0
    tensor = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(device)
    return tensor


def _tensor_to_img(tensor):
    arr = tensor.squeeze(0).permute(1, 2, 0).cpu().numpy()
    arr = (arr + 1.0) * 0.5
    arr = np.clip(arr * 255, 0, 255).astype(np.uint8)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _load_model(ckpt_path, device):
    global _model_cache
    if _model_cache is not None:
        return _model_cache

    ckpt = torch.load(ckpt_path, map_location='cpu', weights_only=False)
    sd = ckpt['state_dict']

    k = int(ckpt.get('hyper_parameters', {}).get('model', {}).get('k', 16))
    model = NeuralPresetStyler(k=k)

    remapped = collections.OrderedDict()
    for key, val in sd.items():
        if key.startswith('net.'):
            new_key = key[4:]
        else:
            new_key = key
        remapped[new_key] = val

    model.load_state_dict(remapped, strict=True)
    model.to(device).eval()
    _model_cache = model

    print(f"[NeuralPreset] Loaded from {ckpt_path} (k={k}, device={device})")
    return model


def neuralpreset_transfer(target_img, reference_img, device="cuda"):
    if device == "cuda" and not torch.cuda.is_available():
        device = "cpu"

    ckpt_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "weights", "neuralpreset", "best.ckpt"
    )

    if not os.path.exists(ckpt_path):
        # Local developer-only checkpoint paths should come from config/runtime path constants in deployments.
        raise FileNotFoundError(f"Checkpoint not found at {ckpt_path}")

    model = _load_model(ckpt_path, device)

    target_tensor = _img_to_tensor(target_img, device)
    reference_tensor = _img_to_tensor(reference_img, device)

    with torch.no_grad():
        style_feats = model.forward_style(reference_tensor)
        out = model.apply_style(target_tensor, style_feats)

    result = _tensor_to_img(out)

    if result.shape[:2] != target_img.shape[:2]:
        result = cv2.resize(result, (target_img.shape[1], target_img.shape[0]))

    return result
