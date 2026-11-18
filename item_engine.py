"""Transparent 500x500 RPG equipment art with a 3px black silhouette outline.

All processing is local. A premade RGBA cutout can be used without rembg.
This module deliberately has no Tk dependency so it can be tested separately.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageChops, ImageFilter

from token_engine import BatchResult, save_original_webp, save_generated

ITEM_SIZE = 500
ITEM_STROKE = 3
ITEM_MODES = ('По центру', 'Диагональ ↗', 'Диагональ ↘')


@dataclass
class ItemAdjust:
    layout: str = 'По центру'
    zoom: float = 100.0
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    flip_horizontal: bool = False
    flip_vertical: bool = False
    clean_noise: bool = True


def item_output_path(source: Path) -> Path:
    """Never clash with the converted source WebP or character token."""
    return source.with_name(f'{source.stem}_item.webp')


def _prepare_subject(image: Image.Image, clean_noise: bool = True) -> Image.Image:
    """Discard only visually negligible alpha speckles before fitting.

    One 1%-opacity line far from an item must not shrink the whole object.
    When disabled, preserve *every* nonzero alpha pixel (legacy behavior).
    The high quality RGBA colors and stronger partially-transparent detail
    are never thresholded or hardened.
    """
    image = image.convert('RGBA')
    if not clean_noise:
        return image
    alpha = image.getchannel('A').point(lambda a: a if a >= 8 else 0)
    image.putalpha(alpha)
    return image


def _subject_bbox(image: Image.Image, clean_noise: bool = True):
    subject = _prepare_subject(image, clean_noise)
    box = subject.getchannel('A').getbbox()
    if box is None:
        raise ValueError('Предмет не найден: прозрачная маска пуста.')
    return (max(0, box[0] - 2), max(0, box[1] - 2),
            min(subject.width, box[2] + 2), min(subject.height, box[3] + 2))


def _major_axis_angle(image: Image.Image) -> float:
    """Estimate the object's main direction with alpha-weighted PCA.

    Coordinates here use +Y upward, like an ordinary geometric drawing.
    The axis is undirected: a sword pointed down is still the same axis.
    """
    mask = image.getchannel('A')
    mask.thumbnail((180, 180), Image.Resampling.BILINEAR)
    width, height = mask.size
    data = mask.tobytes()
    total = sx = sy = sxx = syy = sxy = 0.0
    for row in range(height):
        offset = row * width
        y = float(-row)
        for col in range(width):
            a = data[offset + col]
            # Ignore transparent feathering that would tilt the main axis.
            if a < 55:
                continue
            weight = a / 255.0
            x = float(col)
            total += weight
            sx += weight * x
            sy += weight * y
            sxx += weight * x * x
            syy += weight * y * y
            sxy += weight * x * y
    if total == 0:
        return 0.0
    vx = sxx / total - (sx / total) ** 2
    vy = syy / total - (sy / total) ** 2
    cov = sxy / total - sx * sy / total ** 2
    if abs(vx - vy) < 1e-7 and abs(cov) < 1e-7:
        return 0.0  # nearly round objects have no preferred direction
    return math.degrees(0.5 * math.atan2(2 * cov, vx - vy))


def _orient(cutout: Image.Image, adjust: ItemAdjust) -> Image.Image:
    image = _prepare_subject(cutout, adjust.clean_noise)
    image = image.crop(_subject_bbox(image, clean_noise=False))
    # Mirrors are applied before automatic diagonal alignment so the chosen
    # diagonal direction remains correct after the flip.
    if adjust.flip_horizontal:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if adjust.flip_vertical:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    image.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
    if adjust.layout not in ITEM_MODES:
        raise ValueError(f'Неизвестный способ расположения: {adjust.layout}')
    rotation = float(adjust.rotation)
    if adjust.layout != 'По центру':
        target = 45.0 if adjust.layout == 'Диагональ ↗' else -45.0
        current = _major_axis_angle(image)
        rotation += (target - current + 90.0) % 180.0 - 90.0
    if abs(rotation) > 0.001:
        image = image.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)
        image = image.crop(_subject_bbox(image, clean_noise=False))
    return image


def compose_item(cutout: Image.Image, adjust: ItemAdjust | None = None,
                 size: int = ITEM_SIZE, stroke_px: int = ITEM_STROKE) -> Image.Image:
    """Fit complete item, preserve border, and make all UI sliders meaningful.

    Zoom=100 means the maximum size *at the requested center*, so a zoom
    below 100 visibly shrinks the item. Shifting the center always moves it;
    the engine reduces the permitted fit if necessary to prevent clipping.
    """
    if adjust is None:
        adjust = ItemAdjust()
    if size < 32 or stroke_px < 0 or stroke_px > size // 10:
        raise ValueError('Некорректные параметры размера или обводки.')
    if not math.isfinite(float(adjust.zoom)) or float(adjust.zoom) <= 0:
        raise ValueError('Масштаб предмета должен быть положительным.')
    if not all(math.isfinite(float(value)) for value in (adjust.x, adjust.y, adjust.rotation)):
        raise ValueError('Некорректные параметры положения или поворота.')

    image = _orient(cutout, adjust)
    # Center: the user requested >=5% whitespace, diagonals use a small
    # visual margin. Three black outline pixels need their own safe space.
    margin = round(size * (.05 if adjust.layout == 'По центру' else .018))
    inset = margin + stroke_px
    available = size - 2 * inset
    if available < 1:
        raise ValueError('Недостаточно места для предмета и обводки.')

    # Translate as a desired *center*, not a pixel offset that gets silently
    # clamped to zero whenever the object already fills a dimension. The
    # largest safe scale is recalculated around the selected center.
    travel = size * 0.32
    cx = min(size - inset - 1, max(inset + 1, size * .5 + travel * float(adjust.x) / 100.0))
    cy = min(size - inset - 1, max(inset + 1, size * .5 + travel * float(adjust.y) / 100.0))
    max_fit = min(2 * (cx - inset) / image.width,
                  2 * (size - inset - cx) / image.width,
                  2 * (cy - inset) / image.height,
                  2 * (size - inset - cy) / image.height)
    if max_fit <= 0:
        raise ValueError('Невозможно разместить предмет в заданной позиции.')
    # Old per-item overrides could contain zoom >100. Enforce safety and
    # translate them to the new 100%=largest-safe-fit convention.
    fraction = min(1.0, float(adjust.zoom) / 100.0)
    fit = max_fit * fraction
    new_width = max(1, min(available, int(math.floor(image.width * fit))))
    new_height = max(1, min(available, int(math.floor(image.height * fit))))
    placed = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

    px = max(inset, min(int(round(cx - new_width / 2)), size - inset - new_width))
    py = max(inset, min(int(round(cy - new_height / 2)), size - inset - new_height))
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(placed, dest=(px, py))
    if stroke_px:
        alpha = canvas.getchannel('A')
        contour = alpha.point(lambda a: 255 if a >= 128 else 0)
        dilated = contour.filter(ImageFilter.MaxFilter(2 * stroke_px + 1))
        border_alpha = ImageChops.subtract(dilated, alpha)
        border = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
        border.putalpha(border_alpha)
        border.alpha_composite(canvas)
        return border
    return canvas


def export_items(files, get_cutout, adjustments: dict[Path, ItemAdjust],
                 skip_exists=True, convert_original=True, cancel_event=None,
                 on_progress=None, approved_outputs=None) -> BatchResult:
    result = BatchResult(errors=[], total=len(files))
    for index, source in enumerate(files):
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        if on_progress:
            on_progress(index, len(files), f'[{index + 1}/{len(files)}] {source.name}')
        if convert_original:
            try:
                result.converted += int(save_original_webp(source))
            except Exception as exc:
                result.record_error(source, 'преобразование оригинала', exc)
        destination = item_output_path(source)
        try:
            if skip_exists and destination.exists():
                result.skipped += 1
            else:
                cutout = get_cutout(source)
                image = compose_item(cutout, adjustments[source])
                # Lossless for crisp 3px outlines and correct alpha.
                if save_generated(image, destination, source, 'item', skip_exists=skip_exists,
                                  approved_identity=(approved_outputs or {}).get(destination)):
                    result.created += 1
                else:
                    result.skipped += 1
        except Exception as exc:
            result.record_error(source, 'генерация предмета', exc)
        if on_progress:
            on_progress(index + 1, len(files), f'Выполнено: {index + 1}/{len(files)}')
    return result
