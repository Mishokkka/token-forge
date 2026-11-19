"""Explicit pixel normalization boundary: oriented 8-bit RGB(A), sRGB.

Pillow decodes container-specific sample layouts; this module classifies the
decoded color and alpha independently. Unsupported numeric ranges are errors
rather than silently clamped pixels.
"""
from dataclasses import dataclass
from functools import lru_cache
import io
from PIL import ImageCms, ImageMath, ImageOps

NORMALIZATION_VERSION = 3
HIGH_GRAY_MODES = frozenset(('I;16', 'I;16L', 'I;16B', 'I;16N'))
GRAY_MODES = frozenset(('1', 'L', 'LA', 'La'))
ALPHA_MODES = frozenset(('LA', 'La', 'RGBA', 'RGBa', 'PA'))


@dataclass(frozen=True)
class SampleLayout:
    gray: bool
    high_gray_bits: int | None
    alpha: bool


def _tag_values(raw, tag, default):
    value = raw.tag_v2.get(tag, default)
    return value if isinstance(value, (tuple, list)) else (value,)


def sample_layout(raw):
    """Read storage metadata before EXIF transpose can discard decoder tags."""
    mode = raw.mode
    if mode in ('I', 'F'):
        raise ValueError('32-битные, знаковые и вещественные изображения требуют явной нормализации диапазона. '
                         'Сохрани арт как обычный 8- или 16-битный PNG/TIFF.')
    depth = None
    if mode in HIGH_GRAY_MODES:
        depth = 16
        if raw.format == 'TIFF':
            bits = _tag_values(raw, 258, (16,))
            sample_format = _tag_values(raw, 339, (1,))
            if (len(bits) != 1 or type(bits[0]) is not int or not 9 <= bits[0] <= 16
                    or tuple(sample_format) != (1,)):
                raise ValueError('Неподдерживаемый числовой диапазон TIFF.')
            depth = bits[0]
    # Pillow's 16-bit gray+alpha PNG is expanded to 8-bit RGBA by its
    # decoder. Truecolor RGBA must not be mistaken for grayscale just
    # because the three decoded channels happen to have equal values.
    expanded_gray = (raw.format == 'PNG' and mode == 'RGBA'
                     and any(tile[3] == 'LA;16B' for tile in getattr(raw, 'tile', ())))
    return SampleLayout(mode in GRAY_MODES or depth is not None or expanded_gray,
                        depth, mode in ALPHA_MODES or 'transparency' in raw.info)


def _straight_alpha(image):
    if image.mode == 'RGBa':
        return image.convert('RGBA')
    if image.mode == 'La':
        return image.convert('LA')
    return image


def _alpha_plane(image, layout):
    if not layout.alpha:
        return None
    transparent = image.info.get('transparency')
    if layout.high_gray_bits is not None and isinstance(transparent, int):
        # Compare original samples before quantization.
        return ImageMath.lambda_eval(lambda args: (args['samples'] != args['key']) * 255,
                                     samples=image.convert('I'), key=transparent).convert('L')
    return image.convert('RGBA').getchannel('A')


def _scale_gray(image, bits, target_bits):
    source_max = (1 << bits) - 1
    target_max = (1 << target_bits) - 1
    # Pillow I.point evaluates the affine expression as an integer mapping.
    # Add half a source step for nearest-sample rounding.
    return image.convert('I').point(lambda x: (x * target_max + source_max // 2) / source_max).convert(
        'L' if target_bits == 8 else 'I;16')


@lru_cache(maxsize=1)
def _srgb_bytes():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()


def _cms_input(artwork, layout):
    if layout.high_gray_bits is not None:
        # LittleCMS expects the full 16-bit range, not right-aligned 12-bit
        # samples. Preserve precision when expanding the latter.
        if layout.high_gray_bits == 16:
            return artwork.convert('I').convert('I;16') if artwork.mode == 'I;16N' else artwork
        return _scale_gray(artwork, layout.high_gray_bits, 16)
    if layout.gray:
        if artwork.mode == 'LA':
            return artwork.getchannel('L')
        if artwork.mode == 'RGBA':
            return artwork.getchannel('R')
        return artwork.convert('L')
    if artwork.mode in ('RGB', 'CMYK', 'LAB'):
        return artwork
    return artwork.convert('RGB')


def decode_artwork(raw, *, force_alpha=False):
    """Normalize color and alpha separately, preserving display orientation."""
    layout = sample_layout(raw)
    artwork = _straight_alpha(ImageOps.exif_transpose(raw))
    profile = artwork.info.get('icc_profile')
    alpha = _alpha_plane(artwork, layout) if layout.alpha and (profile or layout.high_gray_bits) else None
    if profile:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            target_bytes = _srgb_bytes()
            target = ImageCms.ImageCmsProfile(io.BytesIO(target_bytes))
            result = ImageCms.profileToProfile(_cms_input(artwork, layout), source, target, outputMode='RGB')
            if alpha is not None:
                result.putalpha(alpha)
            elif force_alpha:
                result = result.convert('RGBA')
            result.info['icc_profile'] = target_bytes
            return result
        except (OSError, ValueError, TypeError, ImageCms.PyCMSError) as exc:
            raise ValueError('Некорректный или неподдерживаемый цветовой профиль ICC. '
                             'Сохрани изображение в sRGB и повтори обработку.') from exc
    output_mode = 'RGBA' if layout.alpha or force_alpha else 'RGB'
    if layout.high_gray_bits is not None:
        gray = _scale_gray(artwork, layout.high_gray_bits, 8)
        result = gray.convert(output_mode)
        if alpha is not None:
            result.putalpha(alpha)
        return result
    return artwork.convert(output_mode)
