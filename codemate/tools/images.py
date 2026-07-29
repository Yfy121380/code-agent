"""图片文件读取与模型输入缓存。

read_file 读取图片时不能把 base64 直接写进 session history，否则一次截图
就可能让本地会话文件和后续 prompt 变得很重。这里负责把图片处理成受限大小
的缓存文件，并返回模型适配层稍后可以转换成 provider image block 的元信息。
"""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from .constants import (
    IMAGE_EXTENSIONS,
    IMAGE_JPEG_QUALITY_MIN,
    IMAGE_JPEG_QUALITY_START,
    IMAGE_MAX_DECODED_PIXELS,
    IMAGE_MAX_HEIGHT,
    IMAGE_MAX_SOURCE_BYTES,
    IMAGE_MAX_WIDTH,
    IMAGE_TARGET_BYTES,
)
from .results import ToolRunOutput


IMAGE_MAGIC_MEDIA_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_media_type(sample):
    """通过文件头判断是否为可直接提交给模型的常见图片格式。"""

    data = bytes(sample or b"")
    for prefix, media_type in IMAGE_MAGIC_MEDIA_TYPES:
        if data.startswith(prefix):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def path_has_image_extension(path):
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def image_media_type_for_file(path):
    try:
        with Path(path).open("rb") as handle:
            return sniff_image_media_type(handle.read(32))
    except OSError:
        return ""


def is_supported_image_file(path):
    return bool(image_media_type_for_file(path))


def _format_bytes(value):
    value = int(value)
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def _media_extension(media_type):
    return {
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(str(media_type), ".img")


def _cache_image_path(agent, source_path, media_type, processed_tag):
    stat = source_path.stat()
    payload = "|".join(
        [
            str(source_path.resolve(strict=False)),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            str(processed_tag),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    media_dir = agent.session_store.media_dir(agent.session["id"])
    media_dir.mkdir(parents=True, exist_ok=True)
    return media_dir / f"read_file_{digest}{_media_extension(media_type)}"


def _rgb_for_jpeg(image):
    from PIL import Image

    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        alpha = image.convert("RGBA")
        background = Image.new("RGBA", alpha.size, (255, 255, 255, 255))
        background.alpha_composite(alpha)
        return background.convert("RGB")
    return image.convert("RGB")


def _encoded_jpeg_under_limit(image):
    current = image
    while True:
        for quality in range(IMAGE_JPEG_QUALITY_START, IMAGE_JPEG_QUALITY_MIN - 1, -5):
            buffer = BytesIO()
            current.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= IMAGE_TARGET_BYTES:
                return data, quality, current.size
        width, height = current.size
        next_size = (max(1, int(width * 0.85)), max(1, int(height * 0.85)))
        if next_size == current.size or min(next_size) < 64:
            return data, quality, current.size
        current = current.resize(next_size)


def prepare_image_read_result(agent, path, display_path):
    """读取图片并返回短文本元信息和模型可用的内部 image block。

    处理策略：
    - 原始文件超过上限直接拒绝，避免把异常大文件读进内存。
    - 解码像素超过上限直接拒绝，避免压缩炸内存。
    - 小图直接缓存原始 bytes；过大或尺寸超限的图片转成 JPEG 并压缩。
    """

    from PIL import Image, ImageOps

    path = Path(path)
    source_size = path.stat().st_size
    if source_size > IMAGE_MAX_SOURCE_BYTES:
        raise ValueError(
            f"image source is too large: {_format_bytes(source_size)} exceeds {_format_bytes(IMAGE_MAX_SOURCE_BYTES)}"
        )

    media_type = image_media_type_for_file(path)
    if not media_type:
        raise ValueError("file is not a supported image format")

    try:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except Exception as exc:
        raise ValueError(f"invalid image file: {exc}") from exc

    source_width, source_height = image.size
    if source_width * source_height > IMAGE_MAX_DECODED_PIXELS:
        raise ValueError(
            f"image has too many pixels: {source_width}x{source_height} exceeds {IMAGE_MAX_DECODED_PIXELS} pixels"
        )

    needs_resize = source_width > IMAGE_MAX_WIDTH or source_height > IMAGE_MAX_HEIGHT
    needs_reencode = needs_resize or source_size > IMAGE_TARGET_BYTES
    processed = False
    quality = None
    if needs_reencode:
        processed = True
        image.thumbnail((IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT))
        encoded, quality, (width, height) = _encoded_jpeg_under_limit(_rgb_for_jpeg(image))
        media_type = "image/jpeg"
        cache_path = _cache_image_path(agent, path, media_type, f"processed-{width}x{height}-{quality}")
        cache_path.write_bytes(encoded)
        output_size = len(encoded)
    else:
        width, height = source_width, source_height
        cache_path = _cache_image_path(agent, path, media_type, "original")
        if not cache_path.exists():
            cache_path.write_bytes(path.read_bytes())
        output_size = source_size

    if output_size > IMAGE_TARGET_BYTES:
        raise ValueError(
            f"processed image is too large: {_format_bytes(output_size)} exceeds {_format_bytes(IMAGE_TARGET_BYTES)}"
        )

    content_lines = [
        f"# {display_path}",
        f"Image file: {display_path}",
        f"media_type: {media_type}",
        f"dimensions: {width}x{height}",
        f"source_dimensions: {source_width}x{source_height}",
        f"source_size: {_format_bytes(source_size)}",
        f"model_size: {_format_bytes(output_size)}",
        f"processed: {'yes' if processed else 'no'}",
    ]
    if quality is not None:
        content_lines.append(f"jpeg_quality: {quality}")

    return ToolRunOutput(
        content="\n".join(content_lines),
        content_blocks=[
            {
                "type": "image",
                "path": str(cache_path),
                "media_type": media_type,
                "width": width,
                "height": height,
                "size_bytes": output_size,
                "source_path": str(path),
                "source_display_path": display_path,
                "source_width": source_width,
                "source_height": source_height,
                "source_size_bytes": source_size,
                "processed": processed,
            }
        ],
        metadata={
            "image_result": True,
            "image_media_type": media_type,
            "image_width": width,
            "image_height": height,
            "image_size_bytes": output_size,
            "image_processed": processed,
        },
    )
