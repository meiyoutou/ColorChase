import os
import numpy as np
from typing import Optional


def export_image(
    img: np.ndarray,
    filepath: str,
    quality: int = 95,
    bit_depth: int = 8,
    icc_profile: Optional[bytes] = None,
) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)

    if ext == '.png':
        _export_png(img, filepath, bit_depth)
    elif ext in ('.jpg', '.jpeg'):
        _export_jpeg(img, filepath, quality)
    elif ext in ('.tif', '.tiff'):
        _export_tiff(img, filepath, bit_depth)
    elif ext == '.webp':
        _export_webp(img, filepath, quality)
    elif ext in ('.heif', '.heic'):
        _export_heif(img, filepath, quality)
    elif ext == '.avif':
        _export_avif(img, filepath, quality)
    elif ext == '.exr':
        _export_exr(img, filepath)
    elif ext == '.jxl':
        _export_jxl(img, filepath, quality)
    else:
        _export_png(img, filepath, bit_depth)

    return filepath


def _ensure_uint8(img: np.ndarray) -> np.ndarray:
    if img.dtype == np.uint8:
        return img
    if img.max() <= 1.0:
        return (np.clip(img, 0, 1) * 255).astype(np.uint8)
    return np.clip(img, 0, 255).astype(np.uint8)


def _ensure_uint16(img: np.ndarray) -> np.ndarray:
    if img.dtype == np.uint16:
        return img
    if img.max() <= 1.0:
        return (np.clip(img, 0, 1) * 65535).astype(np.uint16)
    return np.clip(img, 0, 65535).astype(np.uint16)


def _ensure_float32(img: np.ndarray) -> np.ndarray:
    if img.dtype == np.float32:
        return img
    return img.astype(np.float32) / 255.0 if img.max() > 1.0 else img.astype(np.float32)


def _export_png(img: np.ndarray, filepath: str, bit_depth: int = 8):
    import cv2
    if bit_depth == 16:
        data = _ensure_uint16(img)
        if data.shape[2] == 3:
            data = data[:, :, ::-1]
        cv2.imwrite(filepath, data)
    else:
        data = _ensure_uint8(img)
        if data.shape[2] == 3:
            data = cv2.cvtColor(data, cv2.COLOR_RGB2BGR)
        cv2.imwrite(filepath, data, [cv2.IMWRITE_PNG_COMPRESSION, 3])


def _export_jpeg(img: np.ndarray, filepath: str, quality: int = 95):
    import cv2
    data = _ensure_uint8(img)
    if data.shape[2] == 3:
        data = cv2.cvtColor(data, cv2.COLOR_RGB2BGR)
    cv2.imwrite(filepath, data, [cv2.IMWRITE_JPEG_QUALITY, quality])


def _export_tiff(img: np.ndarray, filepath: str, bit_depth: int = 8):
    import tifffile
    if bit_depth == 16:
        data = _ensure_uint16(img)
    elif bit_depth == 32:
        data = _ensure_float32(img)
    else:
        data = _ensure_uint8(img)
    tifffile.imwrite(filepath, data)


def _export_webp(img: np.ndarray, filepath: str, quality: int = 95):
    import cv2
    data = _ensure_uint8(img)
    if data.shape[2] == 3:
        data = cv2.cvtColor(data, cv2.COLOR_RGB2BGR)
    cv2.imwrite(filepath, data, [cv2.IMWRITE_WEBP_QUALITY, quality])


def _export_heif(img: np.ndarray, filepath: str, quality: int = 90):
    from pillow_heif import register_heif_opener
    register_heif_opener()
    from PIL import Image
    data = _ensure_uint8(img)
    pil_img = Image.fromarray(data, 'RGB')
    pil_img.save(filepath, quality=quality)


def _export_avif(img: np.ndarray, filepath: str, quality: int = 90):
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener(allow_avif=True)
        from PIL import Image
        data = _ensure_uint8(img)
        pil_img = Image.fromarray(data, 'RGB')
        pil_img.save(filepath, quality=quality)
    except Exception:
        import imagecodecs
        data = _ensure_uint8(img)
        encoded = imagecodecs.avif_encode(data, level=quality)
        with open(filepath, 'wb') as f:
            f.write(encoded)


def _export_exr(img: np.ndarray, filepath: str):
    import OpenEXR
    import Imath
    data = _ensure_float32(img)
    h, w = data.shape[:2]
    header = OpenEXR.Header(w, h)
    header['channels'] = {
        'R': Imath.Channel(Imath.PixelType(Imath.PixelType.FLOAT)),
        'G': Imath.Channel(Imath.PixelType(Imath.PixelType.FLOAT)),
        'B': Imath.Channel(Imath.PixelType(Imath.PixelType.FLOAT)),
    }
    exr = OpenEXR.OutputFile(filepath, header)
    r = data[:, :, 0].astype(np.float32).tobytes()
    g = data[:, :, 1].astype(np.float32).tobytes()
    b = data[:, :, 2].astype(np.float32).tobytes()
    exr.writePixels({'R': r, 'G': g, 'B': b})
    exr.close()


def _export_jxl(img: np.ndarray, filepath: str, quality: int = 90):
    import imagecodecs
    data = _ensure_uint8(img)
    encoded = imagecodecs.jpegxl_encode(data, level=quality)
    with open(filepath, 'wb') as f:
        f.write(encoded)
