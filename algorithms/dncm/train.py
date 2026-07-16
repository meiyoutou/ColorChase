import os
import random
import numpy as np
import cv2
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from .model import NeuralPresetPipeline, NormalizationStage, StylizationStage
from core.io.loaders.universal_loader import RAW_EXTS, load_image_bgr


class ColorAugmentationDataset(Dataset):
    def __init__(self, image_dir, image_size=256, num_luts=300):
        self.image_dir = image_dir
        self.image_size = image_size
        # 2026-07-15 调试：用户上传了 RAW 相机原图训练，之前只认 jpg/png/bmp，导致训练集为空。
        # 这里把 RAW 扩展名加进来，让 DNCM/NeuralPreset 训练也能读到这些图。
        _supported_exts = ('.jpg', '.jpeg', '.png', '.bmp') + tuple(RAW_EXTS)
        self.image_files = [f for f in os.listdir(image_dir)
                           if f.lower().endswith(_supported_exts)]
        self.lut_transforms = self._generate_random_luts(num_luts)

    def _generate_random_luts(self, num_luts):
        luts = []
        for _ in range(num_luts):
            lut = self._create_random_lut()
            luts.append(lut)
        return luts

    def _create_random_lut(self, lut_size=17):
        identity = np.arange(256, dtype=np.float32) / 255.0
        lut_3d = np.zeros((lut_size, lut_size, lut_size, 3), dtype=np.float32)

        for r in range(lut_size):
            for g in range(lut_size):
                for b in range(lut_size):
                    r_val = r / (lut_size - 1)
                    g_val = g / (lut_size - 1)
                    b_val = b / (lut_size - 1)

                    shift_r = random.gauss(0, 0.05)
                    shift_g = random.gauss(0, 0.05)
                    shift_b = random.gauss(0, 0.05)

                    lut_3d[r, g, b, 0] = np.clip(r_val + shift_r, 0, 1)
                    lut_3d[r, g, b, 1] = np.clip(g_val + shift_g, 0, 1)
                    lut_3d[r, g, b, 2] = np.clip(b_val + shift_b, 0, 1)

        return lut_3d

    def _apply_color_augmentation(self, img):
        brightness = random.uniform(0.6, 1.4)
        contrast = random.uniform(0.7, 1.3)
        saturation = random.uniform(0.5, 1.5)
        hue_shift = random.uniform(-0.1, 0.1)

        hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV).astype(np.float32)
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * saturation, 0, 255)
        hsv[:, :, 0] = np.clip(hsv[:, :, 0] + hue_shift * 180, 0, 180)
        img_aug = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB).astype(np.float32)

        img_aug = np.clip((img_aug - 128) * contrast + 128, 0, 255)
        img_aug = np.clip(img_aug * brightness, 0, 255)

        return img_aug.astype(np.uint8)

    def __len__(self):
        return len(self.image_files)

    def __getitem__(self, idx):
        img_path = os.path.join(self.image_dir, self.image_files[idx])
        # RAW 用通用加载器（rawpy）解码，普通图也能走同一条路径，省得分两套逻辑。
        try:
            bgr, _ = load_image_bgr(img_path)
            img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        except Exception as exc:
            print(f"[DNCM Dataset] 加载失败，用黑图占位: {img_path} -> {exc}")
            img = np.zeros((self.image_size, self.image_size, 3), dtype=np.uint8)
        img = cv2.resize(img, (self.image_size, self.image_size))

        img_i = self._apply_color_augmentation(img)
        img_j = self._apply_color_augmentation(img)

        img_i = torch.from_numpy(img_i).permute(2, 0, 1).float() / 255.0
        img_j = torch.from_numpy(img_j).permute(2, 0, 1).float() / 255.0

        return img_i, img_j


class NormalizationLoss(nn.Module):
    def __init__(self):
        super(NormalizationLoss, self).__init__()

    def forward(self, normalized_i, normalized_j):
        loss = F.mse_loss(normalized_i, normalized_j)
        return loss


class StylizationLoss(nn.Module):
    def __init__(self):
        super(StylizationLoss, self).__init__()

    def forward(self, stylized, pseudo_stylized):
        loss = F.mse_loss(stylized, pseudo_stylized)
        return loss


import torch.nn.functional as F


def train_normalization_stage(
    image_dir,
    output_dir,
    epochs=50,
    batch_size=8,
    lr=1e-4,
    image_size=256,
    device=None,
    progress_callback=None,
    control_callback=None,
):
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    os.makedirs(output_dir, exist_ok=True)

    dataset = ColorAugmentationDataset(image_dir, image_size=image_size)
    if len(dataset) == 0:
        raise ValueError("训练目录中没有可用图片")
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)

    model = NormalizationStage(encoder_name='simple').to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = NormalizationLoss()

    print(f"[Stage 1] Training Normalization on {device}")
    print(f"  Dataset: {len(dataset)} images, Batch: {batch_size}")

    best_loss = float('inf')
    for epoch in range(epochs):
        total_loss = 0
        count = 0
        total_batches = max(len(dataloader), 1)
        for batch_index, (img_i, img_j) in enumerate(dataloader, start=1):
            if control_callback is not None:
                control_callback()
            img_i, img_j = img_i.to(device), img_j.to(device)

            norm_i, _ = model(img_i, return_mapping=True)
            norm_j, _ = model(img_j, return_mapping=True)

            loss = criterion(norm_i, norm_j)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            count += 1
            if progress_callback is not None:
                current_avg = total_loss / max(count, 1)
                current_epoch = epoch + (batch_index / total_batches)
                progress_callback(current_epoch, epochs, current_avg)

        avg_loss = total_loss / max(count, 1)
        print(f"  Epoch {epoch+1}/{epochs} - Loss: {avg_loss:.6f}")
        if progress_callback is not None:
            progress_callback(epoch + 1, epochs, avg_loss)

        if avg_loss < best_loss:
            best_loss = avg_loss
            save_path = os.path.join(output_dir, "norm_stage_best.pth")
            torch.save(model.state_dict(), save_path)
            print(f"  [OK] Saved best model (loss={best_loss:.6f})")

    final_path = os.path.join(output_dir, "norm_stage_final.pth")
    torch.save(model.state_dict(), final_path)
    print(f"[Stage 1] Done. Best loss: {best_loss:.6f}")
    return model


def train_stylization_stage(
    image_dir,
    norm_model_path,
    output_dir,
    epochs=50,
    batch_size=8,
    lr=1e-4,
    image_size=256,
    device=None,
    progress_callback=None,
    control_callback=None,
):
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    os.makedirs(output_dir, exist_ok=True)

    norm_model = NormalizationStage(encoder_name='simple').to(device)
    norm_model.load_state_dict(torch.load(norm_model_path, map_location=device))
    norm_model.eval()

    dataset = ColorAugmentationDataset(image_dir, image_size=image_size)
    if len(dataset) == 0:
        raise ValueError("训练目录中没有可用图片")
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)

    style_model = StylizationStage(encoder_name='simple').to(device)
    optimizer = torch.optim.Adam(style_model.parameters(), lr=lr)
    criterion = StylizationLoss()

    print(f"[Stage 2] Training Stylization on {device}")

    best_loss = float('inf')
    for epoch in range(epochs):
        total_loss = 0
        count = 0
        total_batches = max(len(dataloader), 1)
        for batch_index, (img_i, img_j) in enumerate(dataloader, start=1):
            if control_callback is not None:
                control_callback()
            img_i, img_j = img_i.to(device), img_j.to(device)

            with torch.no_grad():
                norm_i, _ = norm_model(img_i, return_mapping=True)
                pseudo_stylized, _ = style_model(norm_i, return_mapping=True)

            norm_j, _ = norm_model(img_j, return_mapping=True)
            stylized, _ = style_model(norm_j, return_mapping=True)

            loss = criterion(stylized, pseudo_stylized.detach())

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            count += 1
            if progress_callback is not None:
                current_avg = total_loss / max(count, 1)
                current_epoch = epoch + (batch_index / total_batches)
                progress_callback(current_epoch, epochs, current_avg)

        avg_loss = total_loss / max(count, 1)
        print(f"  Epoch {epoch+1}/{epochs} - Loss: {avg_loss:.6f}")
        if progress_callback is not None:
            progress_callback(epoch + 1, epochs, avg_loss)

        if avg_loss < best_loss:
            best_loss = avg_loss
            save_path = os.path.join(output_dir, "style_stage_best.pth")
            torch.save(style_model.state_dict(), save_path)
            print(f"  [OK] Saved best model (loss={best_loss:.6f})")

    final_path = os.path.join(output_dir, "style_stage_final.pth")
    torch.save(style_model.state_dict(), final_path)
    print(f"[Stage 2] Done. Best loss: {best_loss:.6f}")
    return style_model
