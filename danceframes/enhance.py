"""可选的 CPU 轻量增强与缩略图拼图 (仅 PIL, 无重型模型)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

_THUMB_W = 300      # 拼图单元格宽
_LABEL_H = 30       # 标注条高


def _font(size: int = 18) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 旧版 Pillow 无 size 参数
        return ImageFont.load_default()


def enhance_image(src: Path, dst: Path, jpeg_quality: int = 95) -> None:
    """轻量人像增强: 自动对比度 + 锐化 (可选功能, --enhance 开启)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
    if dst.suffix.lower() == ".jpg":
        img.save(dst, quality=jpeg_quality, subsampling=1)
    else:
        img.save(dst)


def make_grid(items: list[tuple[str, Path]], dst: Path, cols: int = 4) -> Path:
    """把 (标注, 图片) 列表拼成带编号条的网格图, 用于全局精选/总览."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not items:
        raise ValueError("拼图列表为空")

    thumbs: list[tuple[str, Image.Image]] = []
    for label, p in items:
        img = Image.open(p)
        img = ImageOps.exif_transpose(img).convert("RGB")
        h = round(img.height * _THUMB_W / img.width)
        thumbs.append((label, img.resize((_THUMB_W, h))))

    cell_h = max(h.height for _, h in thumbs) + _LABEL_H
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * _THUMB_W, rows * cell_h), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _font(20)

    for i, (label, img) in enumerate(thumbs):
        x = (i % cols) * _THUMB_W
        y = (i // cols) * cell_h
        draw.rectangle([x, y, x + _THUMB_W, y + _LABEL_H - 2], fill=(40, 40, 40))
        draw.text((x + 6, y + 4), label, fill=(255, 220, 60), font=font)
        sheet.paste(img, (x, y + _LABEL_H))

    sheet.save(dst, quality=90)
    return dst
