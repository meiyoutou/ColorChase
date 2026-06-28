import os
import numpy as np
from typing import Optional, Dict, Any, Tuple

RAW_EXTS = {
    '.dng', '.cr2', '.cr3', '.crw', '.nef', '.nrw',
    '.arw', '.srf', '.sr2', '.raf', '.rw2', '.raw', '.rwl',
    '.orf', '.pef', '.ptx', '.3fr', '.fff', '.iiq', '.cap', '.eip',
    '.mef', '.mos', '.mfw', '.x3f', '.dcr', '.kdc', '.k25', '.dcs',
    '.srw', '.erf', '.cs1', '.cs4', '.cs16', '.sti',
    '.bay', '.pxn', '.braw', '.r3d', '.ari', '.cine', '.lfp', '.rwz',
}
PSD_EXTS = {'.psd', '.psb'}
HEIF_EXTS = {'.heif', '.heic'}
AVIF_EXTS = {'.avif'}
JXL_EXTS = {'.jxl'}
EXR_EXTS = {'.exr'}
TIFF_EXTS = {'.tif', '.tiff'}
STANDARD_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.webp', '.ppm', '.pgm', '.pbm', '.tga', '.ras'}
ALL_SUPPORTED = RAW_EXTS | PSD_EXTS | HEIF_EXTS | AVIF_EXTS | JXL_EXTS | EXR_EXTS | TIFF_EXTS | STANDARD_EXTS

FORMAT_INFO = {
    'raw': {'exts': RAW_EXTS, 'name': 'RAW', 'bit_depth': '14-16', 'color': 'linear'},
    'psd': {'exts': PSD_EXTS, 'name': 'PSD/PSB', 'bit_depth': '8-16', 'color': 'sRGB/CMYK'},
    'heif': {'exts': HEIF_EXTS, 'name': 'HEIF/HEIC', 'bit_depth': '8-10', 'color': 'sRGB/HDR'},
    'avif': {'exts': AVIF_EXTS, 'name': 'AVIF', 'bit_depth': '8-12', 'color': 'sRGB/HDR'},
    'jxl': {'exts': JXL_EXTS, 'name': 'JPEG XL', 'bit_depth': '8-16', 'color': 'sRGB/HDR'},
    'exr': {'exts': EXR_EXTS, 'name': 'OpenEXR', 'bit_depth': '16/32 float', 'color': 'linear'},
    'tiff': {'exts': TIFF_EXTS, 'name': 'TIFF', 'bit_depth': '8-32', 'color': 'varies'},
    'standard': {'exts': STANDARD_EXTS, 'name': 'Standard', 'bit_depth': '8', 'color': 'sRGB'},
}

SUPPORTED_EXTENSIONS = ALL_SUPPORTED


def _detect_format(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    for fmt_name, info in FORMAT_INFO.items():
        if ext in info['exts']:
            return fmt_name
    return 'standard'


def _load_raw(filepath: str, mode: str = "preview") -> Tuple[np.ndarray, Dict[str, Any]]:
    import rawpy
    import tempfile
    import shutil
    try:
        raw_file = filepath
        raw_ctx = rawpy.imread(filepath)
    except Exception as path_err:
        print(f"[Loader] rawpy direct read failed (likely CJK path): {path_err}")
        tmp_dir_obj = tempfile.TemporaryDirectory()
        tmp_raw = os.path.join(tmp_dir_obj.name, "raw_temp" + os.path.splitext(filepath)[1].lower())
        shutil.copy2(filepath, tmp_raw)
        raw_file = tmp_raw
        raw_ctx = rawpy.imread(tmp_raw)
    try:
        raw = raw_ctx.__enter__()
        if mode == "preview":
            rgb = raw.postprocess(
                half_size=True,
                output_bps=8,
                use_camera_wb=True,
                no_auto_bright=True,
                output_color=rawpy.ColorSpace.sRGB,
                gamma=(2.2, 4.5),
                demosaic_algorithm=rawpy.DemosaicAlgorithm.LINEAR,
            )
            rgb_f = rgb.astype(np.float32) / 255.0
            bit_depth = 8
            colorspace = 'srgb'
            is_linear = False
        else:
            rgb = raw.postprocess(
                half_size=False,
                output_bps=16,
                use_camera_wb=True,
                no_auto_bright=True,
                output_color=rawpy.ColorSpace.raw,
                gamma=(1, 1),
                demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
            )
            rgb_f = rgb.astype(np.float32) / 65535.0
            bit_depth = 16
            colorspace = 'linear'
            is_linear = True
    finally:
        raw_ctx.__exit__(None, None, None)
        try:
            tmp_dir_obj.cleanup()
        except NameError:
            pass

    meta = {
        'bit_depth': bit_depth,
        'colorspace': colorspace,
        'is_linear': is_linear,
        'format': 'raw',
        'width': rgb.shape[1],
        'height': rgb.shape[0],
        'mode': mode,
    }
    return rgb_f, meta


def _load_psd(filepath: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    from psd_tools import PSDImage
    psd = PSDImage.open(filepath)
    composite = psd.composite()
    if composite.mode == 'CMYK':
        composite = composite.convert('RGB')
    rgb = np.array(composite.convert('RGB'), dtype=np.float32) / 255.0
    meta = {
        'bit_depth': 8,
        'colorspace': 'srgb',
        'is_linear': False,
        'format': 'psd',
        'width': rgb.shape[1],
        'height': rgb.shape[0],
        'layer_count': len(list(psd.descendants())),
    }
    return rgb, meta


def _load_heif(filepath: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    from PIL import Image
    img = Image.open(filepath)
    has_icc = 'icc_profile' in img.info
    rgb = np.array(img.convert('RGB'), dtype=np.float32) / 255.0
    meta = {
        'bit_depth': 10 if img.mode == 'I;16' else 8,
        'colorspace': 'srgb',
        'is_linear': False,
        'format': 'heif',
        'width': rgb.shape[1],
        'height': rgb.shape[0],
        'has_icc': has_icc,
    }
    return rgb, meta


def _load_avif(filepath: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener(allow_avif=True)
        from PIL import Image
        img = Image.open(filepath)
        rgb = np.array(img.convert('RGB'), dtype=np.float32) / 255.0
    except Exception:
        import imagecodecs
        with open(filepath, 'rb') as f:
            data = f.read()
        arr = imagecodecs.avif_decode(data)
        rgb = arr.astype(np.float32) / 255.0
    meta = {
        'bit_depth': 10,
        'colorspace': 'srgb',
        'is_linear': False,
        'format': 'avif',
        'width': rgb.shape[1],
        'height': rgb.shape[0],
    }
    return rgb, meta


def _load_jxl(filepath: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    import imagecodecs
    with open(filepath, 'rb') as f:
        data = f.read()
    arr = imagecodecs.jpegxl_decode(data)
    if arr.dtype == np.uint16:
        rgb = arr.astype(np.float32) / 65535.0
    elif arr.dtype == np.float32:
        rgb = arr.astype(np.float32)
    else:
        rgb = arr.astype(np.float32) / 255.0
    meta = {
        'bit_depth': 16 if arr.dtype == np.uint16 else 8,
        'colorspace': 'srgb',
        'is_linear': False,
        'format': 'jxl',
        'width': rgb.shape[1],
        'height': rgb.shape[0],
    }
    return rgb, meta


def _load_exr(filepath: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    import OpenEXR
    import Imath
    exr_file = OpenEXR.InputFile(filepath)
    header = exr_file.header()
    dw = header['dataWindow']
    w = dw.max.x - dw.min.x + 1
    h = dw.max.y - dw.min.y + 1
    pt = Imath.PixelType(Imath.PixelType.FLOAT)
    channels = header['channels']
    r_str = exr_file.channel('R', pt) if 'R' in channels else b''
    g_str = exr_file.channel('G', pt) if 'G' in channels else b''
    b_str = exr_file.channel('B', pt) if 'B' in channels else b''
    if not r_str:
        r_str = exr_file.channel(list(channels.keys())[0], pt)
        g_str = r_str
        b_str = r_str
    r = np.frombuffer(r_str, dtype=np.float32).reshape(h, w)
    g = np.frombuffer(g_str, dtype=np.float32).reshape(h, w)
    b = np.frombuffer(b_str, dtype=np.float32).reshape(h, w)
    rgb = np.stack([r, g, b], axis=-1)
    rgb = np.clip(rgb, 0, None)
    meta = {
        'bit_depth': 32,
        'colorspace': 'linear',
        'is_linear': True,
        'format': 'exr',
        'width': w,
        'height': h,
    }
    return rgb, meta


def _load_tiff(filepath: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    import tifffile
    img = tifffile.imread(filepath)
    is_linear = False
    if img.dtype == np.uint16:
        rgb = img.astype(np.float32) / 65535.0
        bit_depth = 16
    elif img.dtype == np.float32:
        rgb = img.astype(np.float32)
        bit_depth = 32
        is_linear = True
    elif img.dtype == np.uint8:
        rgb = img.astype(np.float32) / 255.0
        bit_depth = 8
    else:
        rgb = img.astype(np.float32) / 255.0
        bit_depth = 8
    if rgb.ndim == 2:
        rgb = np.stack([rgb] * 3, axis=-1)
    elif rgb.shape[-1] == 4:
        rgb = rgb[:, :, :3]
    elif rgb.shape[-1] == 1:
        rgb = np.repeat(rgb, 3, axis=-1)
    if rgb.shape[-1] > 3:
        rgb = rgb[:, :, :3]
    meta = {
        'bit_depth': bit_depth,
        'colorspace': 'linear' if is_linear else 'srgb',
        'is_linear': is_linear,
        'format': 'tiff',
        'width': rgb.shape[1],
        'height': rgb.shape[0],
    }
    return rgb, meta


def _load_standard(filepath: str) -> Tuple[np.ndarray, Dict[str, Any]]:
    import cv2
    arr = np.fromfile(filepath, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        from PIL import Image
        pil_img = Image.open(filepath).convert('RGB')
        img = np.array(pil_img)
        img = img[:, :, ::-1].copy()
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    meta = {
        'bit_depth': 8,
        'colorspace': 'srgb',
        'is_linear': False,
        'format': 'standard',
        'width': rgb.shape[1],
        'height': rgb.shape[0],
    }
    return rgb, meta


_LOADERS = {
    'raw': _load_raw,
    'psd': _load_psd,
    'heif': _load_heif,
    'avif': _load_avif,
    'jxl': _load_jxl,
    'exr': _load_exr,
    'tiff': _load_tiff,
    'standard': _load_standard,
}


def load_image(filepath: str, target_size: Optional[int] = None, mode: str = "preview") -> Tuple[np.ndarray, Dict[str, Any]]:
    fmt = _detect_format(filepath)
    loader = _LOADERS.get(fmt, _load_standard)
    try:
        if fmt == 'raw':
            rgb, meta = loader(filepath, mode=mode)
        else:
            rgb, meta = loader(filepath)
    except Exception as e:
        try:
            rgb, meta = _load_standard(filepath)
            meta['fallback'] = True
            meta['fallback_error'] = str(e)
        except Exception as e2:
            raise ValueError(f"Cannot load image: {filepath}. Error: {e2}")
    if target_size and max(rgb.shape[:2]) > target_size:
        h, w = rgb.shape[:2]
        scale = target_size / max(h, w)
        new_w, new_h = int(w * scale), int(h * scale)
        import cv2
        rgb_u8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
        resized = cv2.resize(rgb_u8, (new_w, new_h), interpolation=cv2.INTER_AREA)
        rgb = resized.astype(np.float32) / 255.0
        meta['resized'] = True
        meta['original_size'] = (w, h)
    return rgb, meta


def load_image_bgr(filepath: str, target_size: Optional[int] = None, mode: str = "preview") -> Tuple[np.ndarray, Dict[str, Any]]:
    rgb, meta = load_image(filepath, target_size, mode=mode)
    import cv2
    if meta.get('bit_depth') == 16 and mode == 'export':
        bgr = cv2.cvtColor((np.clip(rgb, 0, 1) * 65535).astype(np.uint16), cv2.COLOR_RGB2BGR)
    else:
        bgr = cv2.cvtColor((np.clip(rgb, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    return bgr, meta


def is_supported(filepath: str) -> bool:
    ext = os.path.splitext(filepath)[1].lower()
    return ext in ALL_SUPPORTED
