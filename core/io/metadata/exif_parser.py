from typing import Dict, Any, Optional


def parse_exif(filepath: str) -> Dict[str, Any]:
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, GPSTAGS
        img = Image.open(filepath)
        exif_data = img._getexif()
        if exif_data is None:
            return {}
        result = {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if isinstance(value, bytes):
                try:
                    value = value.decode('utf-8', errors='replace')
                except Exception:
                    continue
            result[tag] = value
        return result
    except Exception:
        return {}


def parse_exif_from_bytes(data: bytes) -> Dict[str, Any]:
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        exif_data = img._getexif()
        if exif_data is None:
            return {}
        from PIL.ExifTags import TAGS
        result = {}
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if isinstance(value, bytes):
                try:
                    value = value.decode('utf-8', errors='replace')
                except Exception:
                    continue
            result[tag] = value
        return result
    except Exception:
        return {}
