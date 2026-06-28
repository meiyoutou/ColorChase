import numpy as np
from typing import Optional, Tuple


def parse_icc_profile(filepath: str) -> Optional[bytes]:
    try:
        from PIL import Image
        img = Image.open(filepath)
        return img.info.get('icc_profile', None)
    except Exception:
        return None


def parse_icc_from_bytes(data: bytes) -> Optional[bytes]:
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        return img.info.get('icc_profile', None)
    except Exception:
        return None


def apply_icc_to_rgb(rgb: np.ndarray, icc_bytes: bytes) -> np.ndarray:
    try:
        from PIL import Image, ImageCms
        src_profile = ImageCms.ImageCmsProfile(ImageCms.core.profile_from_string(icc_bytes))
        dst_profile = ImageCms.createProfile('sRGB')
        pil_img = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8), 'RGB')
        transformed = ImageCms.profileToProfile(pil_img, src_profile, dst_profile)
        return np.array(transformed, dtype=np.float32) / 255.0
    except Exception:
        return rgb
