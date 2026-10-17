"""图片工具：Data URL 解析、MIME 推断与表情包判断。"""

from typing import List, Tuple

import base64
import binascii
import io
import logging

logger = logging.getLogger("plugin.qqpilot.media")

_EMOJI_MAX_SIDE = 512
_EMOJI_SMALL_SIDE = 160


def parse_data_url(url: str) -> Tuple[str, bytes]:
    """解析 ``data:image/...;base64,...`` 格式，返回 MIME 与原始字节。

    非 Data URL 或解析失败时抛出 ``ValueError``。
    """

    if not isinstance(url, str) or not url.startswith("data:"):
        raise ValueError("不是 data URL")
    header, separator, payload = url.partition(",")
    if not separator:
        raise ValueError("data URL 缺少逗号分隔符")
    meta = header[5:]
    mime = "image/png"
    is_base64 = False
    for item in meta.split(";"):
        item = item.strip()
        if not item:
            continue
        if item.lower() == "base64":
            is_base64 = True
        elif item.lower().startswith("image/"):
            mime = item.lower()
    if not is_base64:
        raise ValueError("仅支持 base64 编码的 data URL")
    try:
        binary = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64 图片数据无效") from exc
    if not binary:
        raise ValueError("图片数据为空")
    return mime, binary


def detect_image_mime(binary: bytes, fallback: str = "image/png") -> str:
    """根据文件头推断图片 MIME。"""

    if binary.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if binary.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if binary.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if binary.startswith(b"BM"):
        return "image/bmp"
    if len(binary) >= 12 and binary[:4] == b"RIFF" and binary[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def to_data_url(binary: bytes, mime: str = "image/png") -> str:
    """把图片字节编码成 Data URL。"""

    resolved_mime = detect_image_mime(binary, fallback=mime or "image/png")
    encoded = base64.b64encode(binary).decode("ascii")
    return f"data:{resolved_mime};base64,{encoded}"


def is_emoji_data_url(url: str) -> bool:
    """根据图片尺寸粗略判断是否为表情包。

    Pillow 不可用时一律当作普通图片，避免影响消息投递。
    """

    try:
        from PIL import Image
    except ImportError:
        return False

    try:
        _, binary = parse_data_url(url)
        with Image.open(io.BytesIO(binary)) as image:
            width, height = image.size
    except Exception:
        return False

    if width <= 0 or height <= 0:
        return False
    if max(width, height) <= _EMOJI_SMALL_SIDE:
        return True
    return width == height and width <= _EMOJI_MAX_SIDE


def split_images(urls: List[str]) -> Tuple[List[str], List[str]]:
    """把图片 URL 分成普通图片与表情包两组。"""

    images: List[str] = []
    emojis: List[str] = []
    for url in urls:
        if not isinstance(url, str) or not url:
            continue
        if not url.startswith("data:image/"):
            # 非 Data URL（例如 http 图片地址）保持为普通图片，交给 Host 处理。
            images.append(url)
            continue
        target = emojis if is_emoji_data_url(url) else images
        target.append(url)
    return images, emojis


def resolve_image_extension(mime: str) -> str:
    """把 MIME 映射为安全扩展名。"""

    mapping = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
    }
    return mapping.get((mime or "").lower(), "png")
