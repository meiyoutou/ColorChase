import os
import numpy as np
import torch
import torch.nn as nn
import torchvision
from einops import einsum
from PIL import Image
from torchvision.transforms import v2
from config import MODFLOWS_MODEL_DIR, MODFLOWS_B6_CHECKPOINT, MODFLOWS_B0_CHECKPOINT

_env_root = os.environ.get("COLORCHASE_MODFLOWS_ROOT", "")

CHECKPOINT_DIR = str(MODFLOWS_MODEL_DIR)
if _env_root and os.path.isdir(_env_root):
    CHECKPOINT_DIR = os.path.join(_env_root, "modflows_color_encoder")

B6_CHECKPOINT = str(MODFLOWS_B6_CHECKPOINT)
B0_CHECKPOINT = str(MODFLOWS_B0_CHECKPOINT)

if _env_root and os.path.isdir(_env_root):
    _env_ckpt = os.path.join(_env_root, "modflows_color_encoder")
    if os.path.isdir(_env_ckpt):
        CHECKPOINT_DIR = _env_ckpt
        B6_CHECKPOINT = os.path.join(CHECKPOINT_DIR, "modflows_color_encoder_B6_dim_8195_iter_700000.pt")
        B0_CHECKPOINT = os.path.join(CHECKPOINT_DIR, "modflows_color_encoder_B0_dim_515.pt")


class Encoder(nn.Module):
    def __init__(self, k_dim, input_dim, hidden, output_dim, device, encoder_type="B6"):
        super().__init__()
        self.k_dim = k_dim
        self.input_dim = input_dim
        self.hidden = hidden
        self.output_dim = output_dim

        if encoder_type == "B6":
            self.model = torchvision.models.efficientnet_b6(num_classes=k_dim)
            self.resize = torchvision.models.efficientnet.EfficientNet_B6_Weights.IMAGENET1K_V1.transforms()
        elif encoder_type == "B0":
            self.model = torchvision.models.efficientnet_b0(num_classes=k_dim)
            self.resize = torchvision.models.efficientnet.EfficientNet_B0_Weights.IMAGENET1K_V1.transforms()

        self.device = device
        self.splits = [
            0,
            input_dim * hidden,
            input_dim * hidden + hidden,
            input_dim * hidden + hidden + output_dim * hidden,
            input_dim * hidden + hidden + output_dim * hidden + output_dim,
        ]
        self.to(device)

    def forward(self, im1):
        with torch.no_grad():
            im1 = self.resize(im1)
        return self.model(im1)

    def apply_e(self, e, x, t):
        splits = self.splits
        batch_size = e.shape[0]
        shapes = [
            torch.Size([batch_size, self.hidden, self.input_dim]),
            torch.Size([batch_size, self.hidden]),
            torch.Size([batch_size, self.output_dim, self.hidden]),
            torch.Size([batch_size, self.output_dim]),
        ]
        e0 = e[:, splits[0]:splits[1]].reshape(shapes[0])
        e1 = e[:, splits[1]:splits[2]].reshape(shapes[1])
        e2 = e[:, splits[2]:splits[3]].reshape(shapes[2])
        e3 = e[:, splits[3]:splits[4]].reshape(shapes[3])
        e1 = e1.unsqueeze(1)
        e3 = e3.unsqueeze(1)
        xt = torch.cat([x, t], dim=-1)
        xt = einsum(xt, e0, 'i j k, i n k -> i j n') + e1
        xt = torch.tanh(xt)
        xt = einsum(xt, e2, 'i j k, i n k -> i j n') + e3
        return xt


class NeuralODE(nn.Module):
    def __init__(self, input_dim, device, hidden=1024):
        super().__init__()
        self.device = device
        self.hidden = hidden
        self.input_dim = input_dim + 1
        self.output_dim = input_dim
        self.activation = nn.Tanh()
        self.layer_1 = nn.Linear(self.input_dim, self.hidden, bias=True)
        self.layer_2 = nn.Linear(self.hidden, self.output_dim, bias=True)
        self.shapes = [
            self.layer_1.weight.shape,
            self.layer_1.bias.shape,
            self.layer_2.weight.shape,
            self.layer_2.bias.shape,
        ]
        self.splits = [
            0,
            self.input_dim * hidden,
            self.input_dim * hidden + hidden,
            self.input_dim * hidden + hidden + self.output_dim * hidden,
            self.input_dim * hidden + hidden + self.output_dim * hidden + self.output_dim,
        ]
        self.total_params = sum(p.numel() for p in self.parameters())
        self.to(self.device)

    def set_weights(self, e):
        assert len(e) == self.total_params
        splits = self.splits
        shapes = self.shapes
        e0 = e[splits[0]:splits[1]].reshape(shapes[0])
        e1 = e[splits[1]:splits[2]].reshape(shapes[1])
        e2 = e[splits[2]:splits[3]].reshape(shapes[2])
        e3 = e[splits[3]:splits[4]].reshape(shapes[3])
        mask_dict = {
            'layer_1.weight': e0,
            'layer_1.bias': e1,
            'layer_2.weight': e2,
            'layer_2.bias': e3,
        }
        self.load_state_dict(mask_dict)
        self.to(self.device)

    def forward(self, x, t):
        xt = torch.cat([x, t], dim=1)
        xt = self.layer_1(xt)
        xt = self.activation(xt)
        xt = self.layer_2(xt)
        return xt

    @torch.no_grad()
    def sample(self, x0, N=8, strength=1.0, step_callback=None):
        sample_size = len(x0)
        z = x0.detach().clone()
        dt = 1.0 / N
        for i in range(N):
            t = torch.ones((sample_size, 1)) * i / N
            t = t.to(self.device)
            z = z.to(self.device)
            pred = self.forward(z, t)
            z = z.detach().clone() + pred * dt
            if step_callback is not None:
                step_callback(i + 1, N)
            if i >= int(strength * N) - 1 and strength < 1.0:
                break
        return z.detach().clone()

    @torch.no_grad()
    def inv_sample(self, x0, N=8, strength=1.0, step_callback=None):
        sample_size = len(x0)
        z = x0.detach().clone()
        dt = 1.0 / N
        for i in range(N):
            t = torch.ones((sample_size, 1)) * i / N
            t = t.to(self.device)
            z = z.to(self.device)
            pred = self.forward(z, 1 - t)
            z = z.detach().clone() - pred * dt
            if step_callback is not None:
                step_callback(i + 1, N)
            if i >= int(strength * N) - 1 and strength < 1.0:
                break
        return z.detach().clone()


_encoder_cache = {}


def _get_encoder(encoder_type="B6", device=None):
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    cache_key = f"{encoder_type}_{device}"
    if cache_key in _encoder_cache:
        return _encoder_cache[cache_key]

    if encoder_type == "B6":
        k_dim = 8195
        hidden = 1024
        checkpoint = B6_CHECKPOINT
    else:
        k_dim = 515
        hidden = 64
        checkpoint = B0_CHECKPOINT

    encoder = Encoder(
        k_dim=k_dim, input_dim=4, hidden=hidden,
        output_dim=3, device=device, encoder_type=encoder_type
    )

    if os.path.exists(checkpoint):
        params = torch.load(checkpoint, map_location=device, weights_only=True)
        encoder.load_state_dict(params)
        encoder.eval()
        print(f"[OK] Loaded ModFlows {encoder_type} encoder from {checkpoint}")
    else:
        print(f"[WARN] ModFlows checkpoint not found: {checkpoint}")
        return None

    _encoder_cache[cache_key] = encoder
    return encoder


def modflows_transfer(
    target_img: np.ndarray,
    reference_img: np.ndarray,
    encoder_type: str = "B6",
    steps: int = 16,
    strength: float = 1.0,
    device=None,
    progress_callback=None,
) -> np.ndarray:
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    encoder = _get_encoder(encoder_type, device)
    if encoder is None:
        raise RuntimeError(f"ModFlows encoder not available. Check checkpoint files in {CHECKPOINT_DIR}")

    import cv2

    orig_h, orig_w = target_img.shape[:2]

    def _cb(stage, fraction, message=""):
        if progress_callback is not None:
            progress_callback(stage, fraction, message)

    with torch.no_grad():
        encoder.eval()

        _cb("encode", 0.05, "编码目标图特征...")
        target_pil = Image.fromarray(cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB))
        reference_pil = Image.fromarray(cv2.cvtColor(reference_img, cv2.COLOR_BGR2RGB))

        base_enc_input = _enc_preprocess(target_pil).to(device).unsqueeze(0)
        base_e = encoder(base_enc_input).flatten()
        base_flow = NeuralODE(input_dim=3, hidden=encoder.hidden, device=device)
        base_flow.set_weights(base_e)

        _cb("encode", 0.15, "编码参考图特征...")
        targ_enc_input = _enc_preprocess(reference_pil).to(device).unsqueeze(0)
        targ_e = encoder(targ_enc_input).flatten()
        targ_flow = NeuralODE(input_dim=3, hidden=encoder.hidden, device=device)
        targ_flow.set_weights(targ_e)

        total_pixels = orig_h * orig_w
        MAX_PIXELS_PER_BATCH = 500_000

        if total_pixels <= MAX_PIXELS_PER_BATCH:
            _cb("sample", 0.20, "ODE正向采样 (目标→隐空间)...")
            base_x = np.array(target_pil, dtype=np.float32) / 255
            base_x = base_x.reshape(orig_h * orig_w, 3)
            base_x = torch.tensor(base_x, dtype=torch.float32).to(device)

            def _sample_cb(step, total):
                frac = 0.20 + (step / total) * 0.35
                _cb("sample", frac, f"ODE正向采样 {step}/{total} 步...")

            latent_x = base_flow.sample(base_x, N=steps, strength=strength, step_callback=_sample_cb)

            def _inv_cb(step, total):
                frac = 0.55 + (step / total) * 0.35
                _cb("inv_sample", frac, f"ODE逆向采样 {step}/{total} 步 (参考风格)...")

            _cb("inv_sample", 0.55, "ODE逆向采样 (参考风格)...")
            styled_x = targ_flow.inv_sample(latent_x, N=steps, strength=strength, step_callback=_inv_cb)

            _cb("decode", 0.92, "解码输出...")
            styled_x = styled_x.detach().cpu()
            styled_x = torch.clip(styled_x, 0, 1)
            styled_x = styled_x.reshape((orig_h, orig_w, 3)) * 255
            result_array = np.array(styled_x, dtype=np.uint8)
        else:
            result_array = np.zeros((orig_h, orig_w, 3), dtype=np.float32)
            num_row_chunks = max(1, int(np.ceil(orig_h * orig_w / MAX_PIXELS_PER_BATCH)))
            chunk_h = int(np.ceil(orig_h / num_row_chunks))
            overlap = min(16, chunk_h // 4)

            for ci in range(num_row_chunks):
                chunk_frac = ci / num_row_chunks
                y_start = ci * chunk_h
                y_end = min(y_start + chunk_h, orig_h)

                y_crop_start = max(0, y_start - (overlap if ci > 0 else 0))
                y_crop_end = min(orig_h, y_end + (overlap if ci < num_row_chunks - 1 else 0))

                chunk_img = target_pil.crop((0, y_crop_start, orig_w, y_crop_end))
                chunk_h_actual = y_crop_end - y_crop_start

                _cb("sample", 0.20 + chunk_frac * 0.35, f"ODE正向采样 分块 {ci+1}/{num_row_chunks}...")
                chunk_x = np.array(chunk_img, dtype=np.float32) / 255
                chunk_x = chunk_x.reshape(chunk_h_actual * orig_w, 3)
                chunk_x = torch.tensor(chunk_x, dtype=torch.float32).to(device)

                latent_chunk = base_flow.sample(chunk_x, N=steps, strength=strength)
                _cb("inv_sample", 0.55 + chunk_frac * 0.35, f"ODE逆向采样 分块 {ci+1}/{num_row_chunks}...")
                styled_chunk = targ_flow.inv_sample(latent_chunk, N=steps, strength=strength)

                styled_chunk = styled_chunk.detach().cpu()
                styled_chunk = torch.clip(styled_chunk, 0, 1)
                styled_chunk = styled_chunk.reshape((chunk_h_actual, orig_w, 3)) * 255
                styled_np = np.array(styled_chunk, dtype=np.float32)

                if ci == 0:
                    result_array[y_start:y_end, :, :] = styled_np[y_start - y_crop_start:y_end - y_crop_start, :, :]
                else:
                    blend_start = y_start - y_crop_start
                    blend_end = y_end - y_crop_start
                    fade_len = min(overlap, y_end - y_start)
                    for row in range(fade_len):
                        alpha = row / fade_len
                        src_row = y_start + row
                        if src_row < orig_h:
                            chunk_row = blend_start + row
                            result_array[src_row, :, :] = (
                                result_array[src_row, :, :] * (1 - alpha) +
                                styled_np[chunk_row, :, :] * alpha
                            )
                    core_start = y_start + fade_len
                    core_chunk_start = blend_start + fade_len
                    if core_start < y_end:
                        result_array[core_start:y_end, :, :] = styled_np[core_chunk_start:blend_end, :, :]

            _cb("decode", 0.92, "解码输出...")

        if result_array.dtype != np.uint8:
            result_array = np.clip(result_array, 0, 255).astype(np.uint8)

        result_bgr = cv2.cvtColor(result_array, cv2.COLOR_RGB2BGR)
        _cb("done", 1.0, "ModFlows 追色完成")
        return result_bgr


def modflows_latent_transfer(
    target_img: np.ndarray,
    style_embedding: np.ndarray,
    encoder_type: str = "B6",
    steps: int = 16,
    strength: float = 1.0,
    device=None,
    progress_callback=None,
) -> np.ndarray:
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    encoder = _get_encoder(encoder_type, device)
    if encoder is None:
        raise RuntimeError(f"ModFlows encoder not available. Check checkpoint files in {CHECKPOINT_DIR}")

    import cv2

    orig_h, orig_w = target_img.shape[:2]

    def _cb(stage, fraction, message=""):
        if progress_callback is not None:
            progress_callback(stage, fraction, message)

    with torch.no_grad():
        encoder.eval()

        _cb("encode", 0.05, "编码目标图特征...")
        target_pil = Image.fromarray(cv2.cvtColor(target_img, cv2.COLOR_BGR2RGB))

        base_enc_input = _enc_preprocess(target_pil).to(device).unsqueeze(0)
        base_e = encoder(base_enc_input).flatten()
        base_flow = NeuralODE(input_dim=3, hidden=encoder.hidden, device=device)
        base_flow.set_weights(base_e)

        _cb("encode", 0.15, "加载风格潜变量...")
        targ_e = torch.tensor(style_embedding, dtype=torch.float32).to(device).flatten()
        targ_flow = NeuralODE(input_dim=3, hidden=encoder.hidden, device=device)
        targ_flow.set_weights(targ_e)

        total_pixels = orig_h * orig_w
        MAX_PIXELS_PER_BATCH = 500_000

        if total_pixels <= MAX_PIXELS_PER_BATCH:
            _cb("sample", 0.20, "ODE正向采样 (目标→隐空间)...")
            base_x = np.array(target_pil, dtype=np.float32) / 255
            base_x = base_x.reshape(orig_h * orig_w, 3)
            base_x = torch.tensor(base_x, dtype=torch.float32).to(device)

            def _sample_cb(step, total):
                frac = 0.20 + (step / total) * 0.35
                _cb("sample", frac, f"ODE正向采样 {step}/{total} 步...")

            latent_x = base_flow.sample(base_x, N=steps, strength=strength, step_callback=_sample_cb)

            def _inv_cb(step, total):
                frac = 0.55 + (step / total) * 0.35
                _cb("inv_sample", frac, f"ODE逆向采样 {step}/{total} 步 (风格潜变量)...")

            _cb("inv_sample", 0.55, "ODE逆向采样 (风格潜变量)...")
            styled_x = targ_flow.inv_sample(latent_x, N=steps, strength=strength, step_callback=_inv_cb)

            _cb("decode", 0.92, "解码输出...")
            styled_x = styled_x.detach().cpu()
            styled_x = torch.clip(styled_x, 0, 1)
            styled_x = styled_x.reshape((orig_h, orig_w, 3)) * 255
            result_array = np.array(styled_x, dtype=np.uint8)
        else:
            result_array = np.zeros((orig_h, orig_w, 3), dtype=np.float32)
            num_row_chunks = max(1, int(np.ceil(orig_h * orig_w / MAX_PIXELS_PER_BATCH)))
            chunk_h = int(np.ceil(orig_h / num_row_chunks))
            overlap = min(16, chunk_h // 4)

            for ci in range(num_row_chunks):
                chunk_frac = ci / num_row_chunks
                y_start = ci * chunk_h
                y_end = min(y_start + chunk_h, orig_h)

                y_crop_start = max(0, y_start - (overlap if ci > 0 else 0))
                y_crop_end = min(orig_h, y_end + (overlap if ci < num_row_chunks - 1 else 0))

                chunk_img = target_pil.crop((0, y_crop_start, orig_w, y_crop_end))
                chunk_h_actual = y_crop_end - y_crop_start

                _cb("sample", 0.20 + chunk_frac * 0.35, f"ODE正向采样 分块 {ci+1}/{num_row_chunks}...")
                chunk_x = np.array(chunk_img, dtype=np.float32) / 255
                chunk_x = chunk_x.reshape(chunk_h_actual * orig_w, 3)
                chunk_x = torch.tensor(chunk_x, dtype=torch.float32).to(device)

                latent_chunk = base_flow.sample(chunk_x, N=steps, strength=strength)
                _cb("inv_sample", 0.55 + chunk_frac * 0.35, f"ODE逆向采样 分块 {ci+1}/{num_row_chunks}...")
                styled_chunk = targ_flow.inv_sample(latent_chunk, N=steps, strength=strength)

                styled_chunk = styled_chunk.detach().cpu()
                styled_chunk = torch.clip(styled_chunk, 0, 1)
                styled_chunk = styled_chunk.reshape((chunk_h_actual, orig_w, 3)) * 255
                styled_np = np.array(styled_chunk, dtype=np.float32)

                if ci == 0:
                    result_array[y_start:y_end, :, :] = styled_np[y_start - y_crop_start:y_end - y_crop_start, :, :]
                else:
                    blend_start = y_start - y_crop_start
                    blend_end = y_end - y_crop_start
                    fade_len = min(overlap, y_end - y_start)
                    for row in range(fade_len):
                        alpha = row / fade_len
                        src_row = y_start + row
                        if src_row < orig_h:
                            chunk_row = blend_start + row
                            result_array[src_row, :, :] = (
                                result_array[src_row, :, :] * (1 - alpha) +
                                styled_np[chunk_row, :, :] * alpha
                            )
                    core_start = y_start + fade_len
                    core_chunk_start = blend_start + fade_len
                    if core_start < y_end:
                        result_array[core_start:y_end, :, :] = styled_np[core_chunk_start:blend_end, :, :]

            _cb("decode", 0.92, "解码输出...")

        if result_array.dtype != np.uint8:
            result_array = np.clip(result_array, 0, 255).astype(np.uint8)

        result_bgr = cv2.cvtColor(result_array, cv2.COLOR_RGB2BGR)
        _cb("done", 1.0, "潜变量追色完成")
        return result_bgr


def extract_style_embedding(img_bgr: np.ndarray, encoder_type: str = "B6", device=None) -> np.ndarray:
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    encoder = _get_encoder(encoder_type, device)
    if encoder is None:
        raise RuntimeError(f"ModFlows encoder not available")

    import cv2
    with torch.no_grad():
        encoder.eval()
        pil_img = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
        enc_input = _enc_preprocess(pil_img).to(device).unsqueeze(0)
        embedding = encoder(enc_input).flatten()
        return embedding.detach().cpu().numpy()


def cv2_to_pil_rgb(img: np.ndarray) -> np.ndarray:
    import cv2
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


INPUT_SIZE_B6 = 528
INPUT_SIZE_B0 = 256


def _enc_preprocess(pil_image, encoder_type="B6"):
    input_size = INPUT_SIZE_B6 if encoder_type == "B6" else INPUT_SIZE_B0
    im = pil_image
    im_size = (input_size, input_size)
    crop = min(im.size)
    im = v2.CenterCrop(crop)(im)
    im = v2.Resize(im_size)(im)
    im = np.array(im, dtype=np.float32) / 255
    im = im.reshape((3, input_size, input_size))
    return torch.tensor(im)
