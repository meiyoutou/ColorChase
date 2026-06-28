
import os
import hashlib
import json
import numpy as np
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict, field
from pathlib import Path

from config import STORAGE_CACHE_DIR


@dataclass
class StyleRepresentation:
    """
    风格表示：存储从 preview 图分析出的风格信息，用于全尺寸渲染
    """
    lut: Optional[np.ndarray] = None  # 3D LUT
    tone_curve: Optional[np.ndarray] = None
    color_matrix: Optional[np.ndarray] = None
    local_weight_map: Optional[np.ndarray] = None
    style_embedding: Optional[np.ndarray] = None
    color_stats: Optional[Dict[str, Any]] = field(default_factory=dict)
    algorithm_name: Optional[str] = None
    blend_strength: float = 1.0
    smart_postprocess: bool = False
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """序列化"""
        d = asdict(self)
        # numpy 转 list
        for k in ['lut', 'tone_curve', 'color_matrix', 'local_weight_map', 'style_embedding']:
            if d.get(k) is not None and isinstance(d[k], np.ndarray):
                d[k] = d[k].tolist()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'StyleRepresentation':
        """反序列化"""
        obj = cls()
        for k, v in d.items():
            if k in ['lut', 'tone_curve', 'color_matrix', 'local_weight_map', 'style_embedding'] and v is not None:
                setattr(obj, k, np.array(v))
            else:
                setattr(obj, k, v)
        return obj


class RawCacheManager:
    """
    RAW/DNG 缓存管理器
    """
    def __init__(self, cache_root: Optional[str] = None):
        if cache_root is None:
            cache_root = str(STORAGE_CACHE_DIR / "raw_cache")
        self.cache_root = Path(cache_root)
        self.preview_dir = self.cache_root / "raw_preview"
        self.style_dir = self.cache_root / "style_cache"
        self.full_dir = self.cache_root / "full_decode"
        for d in [self.preview_dir, self.style_dir, self.full_dir]:
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _get_file_key(filepath: str) -> str:
        """生成文件唯一 key"""
        import time
        stat = os.stat(filepath)
        key_str = f"{filepath}_{stat.st_size}_{stat.st_mtime}"
        return hashlib.sha256(key_str.encode()).hexdigest()

    def save_preview(self, filepath: str, rgb: np.ndarray, meta: Dict[str, Any]) -> None:
        """缓存预览图"""
        key = self._get_file_key(filepath)
        np.save(self.preview_dir / f"{key}_rgb.npy", rgb)
        with open(self.preview_dir / f"{key}_meta.json", "w") as f:
            json.dump(meta, f)

    def load_preview(self, filepath: str) -> Optional[Tuple[np.ndarray, Dict[str, Any]]]:
        """加载缓存预览图"""
        key = self._get_file_key(filepath)
        rgb_path = self.preview_dir / f"{key}_rgb.npy"
        meta_path = self.preview_dir / f"{key}_meta.json"
        if rgb_path.exists() and meta_path.exists():
            try:
                rgb = np.load(rgb_path)
                with open(meta_path, "r") as f:
                    meta = json.load(f)
                return rgb, meta
            except Exception:
                return None
        return None

    def save_style(self, filepath: str, style: StyleRepresentation, algorithm_name: str) -> None:
        """缓存风格表示"""
        key = self._get_file_key(filepath)
        style_key = f"{key}_{algorithm_name}"
        with open(self.style_dir / f"{style_key}.json", "w") as f:
            json.dump(style.to_dict(), f)

    def load_style(self, filepath: str, algorithm_name: str) -> Optional[StyleRepresentation]:
        """加载风格表示"""
        key = self._get_file_key(filepath)
        style_key = f"{key}_{algorithm_name}"
        style_path = self.style_dir / f"{style_key}.json"
        if style_path.exists():
            try:
                with open(style_path, "r") as f:
                    d = json.load(f)
                    return StyleRepresentation.from_dict(d)
            except Exception:
                return None
        return None


# 单例
_cache_manager: Optional[RawCacheManager] = None


def get_cache_manager() -> RawCacheManager:
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = RawCacheManager()
    return _cache_manager
