"""TokenForge image pipeline. Tk-independent so composition can be tested offline."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from contextlib import contextmanager, ExitStack, nullcontext
import hashlib
import json
import os
import tempfile
import struct
import traceback
from pathlib import Path
from statistics import median
import math
from local_models import call_rembg, model_revision
from process_lock import process_lock
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageChops

CUTOUT_CACHE_VERSION = 5
MAX_ENCODED_BYTES = 256 * 1024 * 1024
MAX_IMAGE_PIXELS = 64 * 1024 * 1024
MAX_IMAGE_SIDE = 16384
ART_FORMATS = ('PNG', 'JPEG', 'WEBP', 'BMP', 'TIFF')


def _validate_dimensions(width, height, *, ring=False):
    if ring:
        if width != height:
            raise ValueError('PNG-кольцо должно быть квадратным.')
        if width > 4096:
            raise ValueError('Размер рамки превышает 4096 × 4096. Уменьши PNG-кольцо.')
    if (width <= 0 or height <= 0 or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE
            or width * height > MAX_IMAGE_PIXELS):
        raise ValueError('Изображение превышает ресурсный лимит TokenForge (64 МиПикс, сторона 16384).')


def validate_art_dimensions(image):
    _validate_dimensions(*image.size)
    return image


def _webp_dimensions(stream):
    """Inspect the RIFF canvas header, without allocating a native decoder.

    Simple lossy/lossless and extended (including animated) WebP layouts have
    distinct dimension fields. A recognized but invalid header fails closed.
    """
    stream.seek(0)
    try:
        header = stream.read(30)
        if header[:4] != b'RIFF' or header[8:12] != b'WEBP':
            return None
        if len(header) < 20:
            raise ValueError('Повреждённый заголовок WebP.')
        kind = header[12:16]
        length = int.from_bytes(header[16:20], 'little')
        riff_end = 8 + int.from_bytes(header[4:8], 'little')
        if 20 + length + (length & 1) > riff_end:
            raise ValueError('Повреждённый заголовок WebP.')
        if kind == b'VP8X' and length == 10 and len(header) >= 30:
            return (1 + int.from_bytes(header[24:27], 'little'),
                    1 + int.from_bytes(header[27:30], 'little'))
        if kind == b'VP8L' and length >= 5 and len(header) >= 25 and header[20] == 0x2f:
            bits = int.from_bytes(header[21:25], 'little')
            if bits >> 29 == 0:
                return (1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff))
        if (kind == b'VP8 ' and length >= 10 and len(header) >= 30
                and not header[20] & 1 and header[23:26] == b'\x9d\x01\x2a'):
            return (int.from_bytes(header[26:28], 'little') & 0x3fff,
                    int.from_bytes(header[28:30], 'little') & 0x3fff)
        raise ValueError('Повреждённый или неподдерживаемый заголовок WebP.')
    finally:
        stream.seek(0)


@contextmanager
def open_art(source, *, formats=ART_FORMATS, ring=False):
    """Validate before WebP decoder creation, then verify decoded dimensions.

    A pathname is opened once: header inspection and Pillow see the same
    handle (or an existing immutable snapshot for captured artwork).
    """
    context = open(source, 'rb') if isinstance(source, (str, bytes, os.PathLike)) else nullcontext(source)
    with context as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() > MAX_ENCODED_BYTES:
            raise OSError('Файл превышает лимит TokenForge 256 МиБ.')
        dimensions = _webp_dimensions(stream)
        if dimensions is not None:
            _validate_dimensions(*dimensions, ring=ring)
        # Pillow also keeps its own decompression-bomb limit.
        with Image.open(stream, formats=formats) as image:
            _validate_dimensions(*image.size, ring=ring)
            yield image

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}


@dataclass
class Adjust:
    zoom: float = 100.0       # percent of automatically selected zoom
    x: float = 0.0            # token-width percent
    y: float = 0.0            # token-height percent
    gap: float = 11.0         # head-to-top gap, token-height percent


def stat_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_stamp(path: Path):
    """Cheap metadata observation at a UI click; never read file contents."""
    try:
        return stat_signature(path.stat())
    except FileNotFoundError:
        return None


def _fingerprint(source: Path, snapshot=None) -> tuple:
    resolved = Path(source).resolve()
    signature = stat_signature
    path_before = resolved.stat()
    if path_before.st_size > MAX_ENCODED_BYTES:
        raise OSError('Файл превышает лимит TokenForge 256 МиБ.')
    with resolved.open('rb') as stream:
        before = os.fstat(stream.fileno())
        hasher = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            total += len(chunk)
            if total > MAX_ENCODED_BYTES:
                raise OSError('Файл превышает лимит TokenForge 256 МиБ.')
            hasher.update(chunk)
            if snapshot is not None:
                snapshot.write(chunk)
        digest = hasher.hexdigest()
        after = os.fstat(stream.fileno())
        path_after = resolved.stat()
    # On Windows fstat and stat need not expose identical inode/device IDs.
    # Compare each representation with its own earlier snapshot.
    if (signature(before) != signature(after)
            or signature(path_before) != signature(path_after)
            or (after.st_size, after.st_mtime_ns) != (path_after.st_size, path_after.st_mtime_ns)):
        raise OSError(f'Исходный файл изменился во время обработки: {source.name}')
    if total != path_after.st_size:
        raise OSError(f'Размер исходного файла изменился: {source.name}')
    return str(resolved), path_after.st_size, path_after.st_mtime_ns, digest


def source_identity(source: Path) -> tuple:
    return _fingerprint(source)


@contextmanager
def fingerprinted_snapshot(path: Path):
    """Capture the content and its fingerprint in a single source-file read."""
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode='w+b') as snapshot:
        identity = _fingerprint(path, snapshot)
        snapshot.seek(0)
        yield identity, snapshot


def assert_source_identity(source: Path, identity: tuple) -> None:
    if source_identity(source) != identity:
        raise OSError(f'Исходный файл изменился во время обработки: {source.name}')


class DiskCutoutCache:
    """Lossless persistent cache. No image extraction rerun for unchanged inputs."""

    def __init__(self, directory: Path, *, max_bytes=2 * 1024**3, max_entries=4096):
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._last_prune = None
        self._bytes_since_prune = 0

    def path_for(self, source: Path, model: str, keep_alpha: bool, edge_mode: str,
                 *, identity: tuple | None = None, revision=None) -> Path:
        version = identity or source_identity(source)
        revision = model_revision(model) if revision is None else revision
        key = [CUTOUT_CACHE_VERSION, version[0], version[-1],
               model, bool(keep_alpha), edge_mode, revision]
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode('utf-8')).hexdigest()
        return self.directory / f'{digest}.png'

    def load(self, source: Path, model: str, keep_alpha: bool, edge_mode: str,
             *, identity: tuple | None = None, revision=None) -> Image.Image | None:
        path = self.path_for(source, model, keep_alpha, edge_mode, identity=identity, revision=revision)
        try:
            with open_art(path, formats=('PNG',)) as stored:
                image = stored.convert('RGBA')
            try:
                os.utime(path, None)
            except OSError:
                pass
            return image
        except FileNotFoundError:
            return None
        except (OSError, ValueError, Image.DecompressionBombError):
            # Never unlink a name after a failed read: another process may
            # already have replaced it with a valid entry.
            return None

    def save(self, source: Path, model: str, keep_alpha: bool,
             edge_mode: str, cutout: Image.Image, *, identity: tuple | None = None,
             revision=None) -> None:
        destination = self.path_for(source, model, keep_alpha, edge_mode, identity=identity, revision=revision)
        validate_art_dimensions(cutout)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', dir=destination.parent,
                                             prefix='.tmp-', delete=False) as tmp:
                temporary = Path(tmp.name)
                rgba = cutout if cutout.mode == 'RGBA' else cutout.convert('RGBA')
                rgba.save(tmp, format='PNG', compress_level=1)
            if identity is not None:
                assert_source_identity(source, identity)
            encoded_bytes = temporary.stat().st_size
            if encoded_bytes <= self.max_bytes:
                os.replace(temporary, destination)
                self._bytes_since_prune += encoded_bytes
                self._maybe_prune()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _maybe_prune(self):
        import time
        now = time.monotonic()
        if (self._last_prune is None or now - self._last_prune >= 30
                or self._bytes_since_prune >= min(self.max_bytes // 8, 128 * 1024**2)):
            try:
                self.prune()
                self._last_prune = now
                self._bytes_since_prune = 0
            except OSError:
                pass  # Cache maintenance never blocks successful exports.

    def prune(self):
        """Best-effort LRU of only our cache names; never touch source images."""
        with process_lock(self.directory / '.maintenance', timeout=0):
            entries = []
            for path in self.directory.glob('*.png'):
                if not _valid_digest(path.stem) or path.is_symlink():
                    continue
                try:
                    st = path.stat()
                    if path.is_file():
                        entries.append((st.st_mtime_ns, st.st_size, path))
                except OSError:
                    continue
            total, count = sum(item[1] for item in entries), len(entries)
            for stamp, size, path in sorted(entries):
                if total <= self.max_bytes and count <= self.max_entries:
                    break
                try:
                    st = path.stat()
                    if (st.st_mtime_ns, st.st_size) == (stamp, size):
                        path.unlink()
                        total -= size
                        count -= 1
                except OSError:
                    continue


@contextmanager
def source_snapshot(path: Path, identity: tuple):
    """Decode the same bytes whose digest identifies the source version.

    A bounded in-memory spool rolls large inputs onto disk. Even if an
    external writer changes and then restores the pathname during decoding,
    the bytes passed to Pillow cannot change under the decoder.
    """
    digest = hashlib.sha256()
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode='w+b') as snapshot:
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                if snapshot.tell() + len(chunk) > min(identity[1], MAX_ENCODED_BYTES):
                    raise OSError(f'Исходный файл изменился во время чтения: {path.name}')
                digest.update(chunk)
                snapshot.write(chunk)
        if snapshot.tell() != identity[1] or digest.hexdigest() != identity[-1]:
            raise OSError(f'Исходный файл изменился во время чтения: {path.name}')
        snapshot.seek(0)
        yield snapshot


def decode_artwork(raw, *, force_alpha=False):
    """Normalize tagged artwork to sRGB; preserve alpha independently of color.

    Never attach a CMYK/grayscale source profile to converted RGB data.
    Invalid embedded profiles fail with an actionable per-image error.
    """
    import io
    from PIL import ImageCms
    # Pillow exposes a 16-bit grayscale+alpha PNG as RGBA. Capture the
    # decoder's original sample layout before exif_transpose loads it.
    gray_alpha16_png = (raw.format == 'PNG' and raw.mode == 'RGBA'
                        and any(tile[3] == 'LA;16B' for tile in getattr(raw, 'tile', ())))
    artwork = ImageOps.exif_transpose(raw)
    has_alpha = 'A' in artwork.getbands() or 'transparency' in artwork.info
    grayscale16 = artwork.mode in ('I;16', 'I;16L', 'I;16B')
    profile = artwork.info.get('icc_profile')
    alpha = None
    if has_alpha and (profile or grayscale16):
        transparent = artwork.info.get('transparency')
        if grayscale16 and isinstance(transparent, int):
            # Test the original sample, not its quantized RGB equivalent.
            from PIL import ImageMath
            alpha = ImageMath.lambda_eval(lambda args: (args['samples'] != args['key']) * 255,
                                          samples=artwork.convert('I'), key=transparent).convert('L')
        else:
            alpha = artwork.convert('RGBA').getchannel('A')
    if profile:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            target = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
            if artwork.mode in ('RGB', 'CMYK', 'L', 'LAB', 'I;16', 'I;16L', 'I;16B'):
                color = artwork
            elif artwork.mode == '1':
                color = artwork.convert('L')
            elif artwork.mode == 'LA':
                color = artwork.getchannel('L')
            elif gray_alpha16_png:
                # The decoder already expanded gray into three equal 8-bit
                # channels. CMS needs one gray channel; alpha is independent.
                color = artwork.getchannel('R')
            else:
                color = artwork.convert('RGB')
            result = ImageCms.profileToProfile(color, source, target, outputMode='RGB')
            if alpha is not None:
                result.putalpha(alpha)
            elif force_alpha:
                result = result.convert('RGBA')
            result.info['icc_profile'] = target.tobytes()
            return result
        except (OSError, ValueError, TypeError, ImageCms.PyCMSError) as exc:
            raise ValueError('Некорректный или неподдерживаемый цветовой профиль ICC. '
                             'Сохрани изображение в sRGB и повтори обработку.') from exc
    # Pillow's ordinary 16-bit -> RGB conversion clips values above 255.
    # Scale the sample range rather than saturating most grayscale pixels.
    if grayscale16:
        gray = artwork.convert('I').point(lambda value: (value + 128) / 257).convert('L')
        result = gray.convert('RGBA' if has_alpha or force_alpha else 'RGB')
        if alpha is not None:
            result.putalpha(alpha)
        return result
    return artwork.convert('RGBA' if has_alpha or force_alpha else 'RGB')


class BorrowedStream:
    """File API proxy with a borrowed (not transferred) lifetime."""
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def close(self):
        pass


class CapturedArtwork:
    """One lazily materialized, digest-verified byte snapshot per export.

    Skip-only jobs do not spool the source. Copy and extraction decode the
    same immutable bytes. Publication guards still hash the live source.
    """
    def __init__(self, path: Path, identity: tuple):
        self.path = path
        self.identity = identity
        self._stack = ExitStack()
        self._stream = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)

    @contextmanager
    def open(self):
        if self._stream is None:
            self._stream = self._stack.enter_context(source_snapshot(self.path, self.identity))
        self._stream.seek(0)
        # Pillow must not acquire ownership of the shared file object.
        with open_art(BorrowedStream(self._stream)) as image:
            yield image

    def decode(self):
        with self.open() as image:
            return decode_artwork(image, force_alpha=True)


def read_image(path: Path, *, identity: tuple | None = None) -> Image.Image:
    if identity is None:
        if path.stat().st_size > MAX_ENCODED_BYTES:
            raise OSError('Файл превышает лимит TokenForge 256 МиБ.')
        with open_art(path) as img:
            return decode_artwork(img, force_alpha=True)
    with source_snapshot(path, identity) as snapshot:
        with open_art(snapshot) as img:
            return decode_artwork(img, force_alpha=True)


def already_transparent(img: Image.Image) -> bool:
    # Check real alpha; RGBA JPEG conversions have a fully opaque alpha channel.
    return img.getchannel('A').getextrema()[0] < 250


def remove_background(img: Image.Image, session, edge_mode: str = 'Очистка краёв') -> Image.Image:
    # Never hard-threshold the soft mask; fine hair depends on partial alpha.
    kwargs = {'session': session}
    if edge_mode == 'Очистка краёв':
        kwargs['decontaminate'] = True
    elif edge_mode == 'Alpha matting (медленно)':
        kwargs['alpha_matting'] = True
    elif edge_mode != 'Обычный':
        raise ValueError(f'Неизвестный режим краёв: {edge_mode}')
    result = call_rembg('remove', img.convert('RGB'), **kwargs)
    return result.convert('RGBA')


def _ring_thickness(barrier: Image.Image) -> int:
    """Estimate ring thickness in pixels from the alpha-defined contour."""
    def runs(values):
        result, length = [], 0
        for value in list(values) + [False]:
            if value:
                length += 1
            elif length:
                result.append(length)
                length = 0
        return result

    alpha = barrier
    samples = []
    row_y = alpha.height // 2
    col_x = alpha.width // 2
    row = [alpha.getpixel((x, row_y)) >= 55 for x in range(alpha.width)]
    col = [alpha.getpixel((col_x, y)) >= 55 for y in range(alpha.height)]
    samples.extend(runs(row))
    samples.extend(runs(col))
    if not samples:
        return max(1, min(alpha.width, alpha.height) // 25)
    return max(1, int(round(median(samples))))


def square_dilate(mask: Image.Image, radius: int) -> Image.Image:
    """Exact square maximum filter using separable, logarithmic shifts."""
    result = mask
    for axis in (0, 1):
        covered = 0
        while covered < radius:
            step = min(covered + 1, radius - covered)
            combined = result
            for sign in (-1, 1):
                shifted = Image.new('L', result.size)
                offset = (sign * step, 0) if axis == 0 else (0, sign * step)
                shifted.paste(result, offset)
                combined = ImageChops.lighter(combined, shifted)
            result = combined
            covered += step
    return result


def frame_interior(frame: Image.Image) -> Image.Image:
    """Build the character clip mask for the frame.

    The visible cutout should extend *under* the ring so there is no thin
    transparent gap where the character is supposed to disappear behind it.
    This mask therefore reaches to about the middle of the ring thickness,
    while still remaining fully clipped by the outer frame silhouette.
    """
    alpha = frame.getchannel('A')
    barrier = alpha.point(lambda p: 128 if p >= 55 else 0)
    barrier = barrier.filter(ImageFilter.MaxFilter(3))
    center = (frame.width // 2, frame.height // 2)
    if barrier.getpixel(center) != 0:
        raise ValueError('У рамки непрозрачная середина. Нужна рамка с прозрачным центром.')

    interior = barrier.copy()
    ImageDraw.floodfill(interior, center, 255, thresh=0)
    inside = interior.point(lambda p: 255 if p == 255 else 0)
    bbox = inside.getbbox()
    if not bbox:
        raise ValueError('Не удалось выделить внутреннюю область рамки.')
    if bbox[0] == 0 or bbox[1] == 0 or bbox[2] == frame.width or bbox[3] == frame.height:
        raise ValueError('Рамка разомкнута. Используй PNG с непрерывным кольцом.')

    outer_shape = _enclosed_shape(barrier)
    # The validation barrier is dilated by one pixel to seal minor gaps.
    # Clip to the actual ring support, not that padded exterior.
    support = alpha.point(lambda p: 128 if p else 0)
    silhouette = _enclosed_shape(support)
    if silhouette.getpixel(center) == 0:
        # A small opening was closed by the validation barrier. Keep the
        # enclosed interior and actual ring pixels, never the padded fringe.
        silhouette = ImageChops.lighter(inside, alpha.point(lambda p: 255 if p else 0))
    thickness = _ring_thickness(alpha)
    under_ring = max(1, int(round(thickness * 0.5)))
    clip_mask = square_dilate(inside, under_ring)
    clip_mask = ImageChops.multiply(clip_mask, outer_shape)
    feathered = clip_mask.filter(ImageFilter.GaussianBlur(.6))
    return ImageChops.multiply(feathered, silhouette)


def _enclosed_shape(barrier: Image.Image) -> Image.Image:
    outside = barrier.copy()
    # Fill every exterior component touching an edge, even when a corner is opaque.
    frame = barrier
    for x in range(frame.width):
        for y in (0, frame.height - 1):
            if outside.getpixel((x, y)) == 0:
                ImageDraw.floodfill(outside, (x, y), 255, thresh=0)
    for y in range(frame.height):
        for x in (0, frame.width - 1):
            if outside.getpixel((x, y)) == 0:
                ImageDraw.floodfill(outside, (x, y), 255, thresh=0)
    return outside.point(lambda p: 0 if p == 255 else 255)


def load_frame(path: Path):
    if path.stat().st_size > MAX_ENCODED_BYTES:
        raise OSError('Файл превышает лимит TokenForge 256 МиБ.')
    with open_art(path, ring=True) as raw:
        frame = decode_artwork(raw, force_alpha=True)
    return frame, frame_interior(frame)


def silhouette_geometry(cutout: Image.Image):
    """Find rough head center and apparent head width without face detection.

    Fantasy art may have crowns/horns/helmets that defeat face detectors.
    Sampling the upper slice of the alpha silhouette gives an editable default.
    """
    alpha = cutout.getchannel('A')
    solid = alpha.point(lambda p: 255 if p >= 90 else 0)
    bbox = solid.getbbox()
    if bbox is None:
        raise ValueError('Персонаж не найден: результат удаления фона пуст.')
    x0, y0, x1, y1 = bbox
    height = max(1, y1 - y0)
    width = max(1, x1 - x0)
    # Upper 3%-14% of the visible figure. If it is full-body art, this
    # samples the head; if a bust, typically the hair/forehead region.
    widths, centers = [], []
    for fraction in (.035, .05, .065, .08, .095, .11, .125, .14):
        row_y = min(y1 - 1, y0 + int(height * fraction))
        row = solid.crop((0, row_y, cutout.width, row_y + 1)).getbbox()
        if row:
            l, _, r, _ = row
            if r - l >= 3:
                widths.append(r - l)
                centers.append((l + r) * .5)
    if not widths:
        return float((x0 + x1) / 2), float(width * .5), float(y0)
    head_width = max(float(median(widths)), width * .16)
    return float(median(centers)), head_width, float(y0)


@dataclass(frozen=True)
class PreparedTokenFrame:
    size: int
    interior: Image.Image
    frame: Image.Image
    visible: Image.Image


def prepare_token_frame(frame, interior, out_size):
    """Prepare the invariant ring layers once per frame and output size."""
    inner = interior.resize((out_size, out_size), Image.Resampling.LANCZOS)
    support = interior.point(lambda a: 255 if a else 0).resize(
        (out_size, out_size), Image.Resampling.NEAREST)
    inner = ImageChops.multiply(inner, support)
    resized_frame = frame.resize((out_size, out_size), Image.Resampling.LANCZOS)
    visible = ImageChops.invert(resized_frame.getchannel('A'))
    return PreparedTokenFrame(out_size, inner, resized_frame, visible)


def compose(cutout: Image.Image, frame: Image.Image, interior: Image.Image,
            out_size: int = 256, adjust: Adjust | None = None, *, geometry=None,
            prepared_frame=None) -> Image.Image:
    """Place cutout against a ring, clipping everything outside its interior."""
    if adjust is None:
        adjust = Adjust()
    center_x, head_width, top_y = silhouette_geometry(cutout) if geometry is None else geometry
    # Typical tabletop portrait has hair/head ~40% of token width.
    base_scale = .40 * out_size / max(head_width, 1)
    zoom, x, y, gap = map(float, (adjust.zoom, adjust.x, adjust.y, adjust.gap))
    if not math.isfinite(zoom) or zoom <= 0:
        raise ValueError('Масштаб токена должен быть положительным и конечным.')
    if not all(math.isfinite(v) for v in (x, y, gap)):
        raise ValueError('Положение токена должно быть конечным.')
    scale = base_scale * (zoom / 100)
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('Масштаб токена вне допустимого диапазона.')
    ox = out_size * .5 - center_x * scale + x / 100 * out_size
    oy = out_size * gap / 100 - top_y * scale + y / 100 * out_size

    # Crop at source resolution first so huge source images never create a
    # massive intermediate image when the automatic zoom is high.
    if not all(math.isfinite(v) for v in (ox, oy)):
        raise ValueError('Положение токена вне допустимого диапазона.')
    def bound(value, length, *, start):
        value = max(0.0, min(float(length), value))
        return (max(0, math.floor(value) - 2) if start
                else min(length, math.ceil(value) + 2))
    left = bound(-ox / scale, cutout.width, start=True)
    upper = bound(-oy / scale, cutout.height, start=True)
    right = bound((out_size - ox) / scale, cutout.width, start=False)
    lower = bound((out_size - oy) / scale, cutout.height, start=False)
    image_layer = Image.new('RGBA', (out_size, out_size), (0, 0, 0, 0))
    if right > left and lower > upper:
        cropped = cutout.crop((left, upper, right, lower))
        target_size = (max(1, int(round((right - left) * scale))),
                       max(1, int(round((lower - upper) * scale))))
        if max(target_size) > max(4096, out_size * 8):
            raise ValueError('Масштаб токена слишком велик для безопасной компоновки.')
        scaled = cropped.resize(target_size, Image.Resampling.LANCZOS)
        image_layer.paste(scaled, (int(round(ox + left * scale)), int(round(oy + upper * scale))))
    prepared = prepare_token_frame(frame, interior, out_size) if prepared_frame is None else prepared_frame
    if prepared.size != out_size:
        raise ValueError('Размер подготовленной рамки не соответствует размеру токена.')
    image_layer.putalpha(ImageChops.multiply(image_layer.getchannel('A'), prepared.interior))
    visible = ImageChops.multiply(image_layer.getchannel('A'), prepared.visible)
    if visible.getbbox() is None:
        raise ValueError('Персонаж полностью вне видимой области рамки. Измени положение или масштаб.')
    token = Image.alpha_composite(image_layer, prepared.frame)
    return token


def _publish_new(temporary: Path, path: Path) -> bool:
    try:
        if os.name == 'nt':
            os.rename(temporary, path)
        else:
            os.link(temporary, path)
    except FileExistsError:
        return False
    return True


@dataclass(frozen=True)
class Publication:
    created: bool
    warnings: tuple[str, ...] = ()

    def __bool__(self):
        return self.created


def windows_extended_path(path):
    value = str(path.absolute())
    if value.startswith('\\\\?\\'):
        return value
    if value.startswith('\\\\'):
        return '\\\\?\\UNC\\' + value[2:]
    return '\\\\?\\' + value


def _windows_replace(temporary, destination, backup):
    import ctypes
    from ctypes import wintypes
    replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
    replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
    replace.restype = wintypes.BOOL
    if not replace(windows_extended_path(destination), windows_extended_path(temporary), windows_extended_path(backup), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _restore_backup(backup, destination):
    if not backup.exists():
        return 'Резервный файл не найден.'
    try:
        if _publish_new(backup, destination):
            backup.unlink(missing_ok=True)
            return 'Предыдущий результат восстановлен.'
    except OSError as exc:
        return f'Не удалось восстановить предыдущий результат: {exc}. Резервная копия: {backup}'
    return f'Имя уже занято; резервная копия сохранена: {backup}'


def _posix_exchange(first: Path, second: Path) -> None:
    """Swap two names atomically or fail without changing either name.

    Linux renameat2(RENAME_EXCHANGE) and macOS renamex_np(RENAME_SWAP)
    provide this contract. A filesystem without swap support is refused.
    No move-away/restore sequence or hard-link assumption is needed.
    """
    import ctypes
    import errno
    import sys
    libc = ctypes.CDLL(None, use_errno=True)
    a, b = os.fsencode(first), os.fsencode(second)
    if sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        swap = libc.renameat2
        swap.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        swap.restype = ctypes.c_int
        result = swap(-100, a, -100, b, 2)
    elif sys.platform == 'darwin' and hasattr(libc, 'renamex_np'):
        swap = libc.renamex_np
        swap.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        swap.restype = ctypes.c_int
        result = swap(a, b, 2)
    else:
        raise OSError(errno.ENOTSUP, 'Файловая система не поддерживает безопасную замену.')
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(second))


def _replace_posix(temporary, destination, backup):
    # Move only our freshly encoded file to private recovery storage before
    # exchanging it. After a successful exchange, backup is the exact
    # displaced directory entry. Never delete it on an exchange success.
    os.rename(temporary, backup)
    try:
        _posix_exchange(backup, destination)
    except BaseException as exc:
        try:
            backup.unlink(missing_ok=True)
        except OSError as cleanup:
            exc.add_note(f'Не удалось удалить временный файл {backup}: {cleanup}')
        raise


def _replace_preserving_displaced(temporary: Path, destination: Path, expected: tuple) -> tuple[str, ...]:
    """Publish without silently discarding an unexpected displaced file."""
    import uuid
    recovery = destination.parent / '.tokenforge-recovery'
    if os.name == 'nt':
        recovery = Path(windows_extended_path(recovery))
    recovery.mkdir(exist_ok=True)
    backup = recovery / (uuid.uuid4().hex + destination.suffix)
    if os.name == 'nt':
        try:
            _windows_replace(temporary, destination, backup)
        except OSError as exc:
            detail = (_restore_backup(backup, destination) if getattr(exc, 'winerror', None) == 1177
                      else f'Резервная копия, если создана: {backup}')
            raise OSError(f'Не удалось заменить {destination.name}: {exc}. {detail}') from exc
    else:
        _replace_posix(temporary, destination, backup)
    # Publication has succeeded. Any subsequent issue is a warning, never
    # an assertion that no output was produced.
    try:
        if source_identity(backup)[-1] != expected[-1]:
            return (f'Другой редактор изменил файл. Вытесненная версия сохранена: {backup}',)
        backup.unlink()
    except OSError as exc:
        return (f'Не удалось проверить или удалить резервную копию {backup}: {exc}',)
    return ()


def atomic_write(path: Path, writer, *, replace: bool = True, before_publish=None,
                 expected_destination=None, after_publish=None,
                 late_conflict_hint='Повтори обработку с отключённым пропуском готовых изображений.') -> Publication:
    """Publish only a complete, closed file and distinguish success with warnings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    warnings = []
    created = False
    failure = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent,
                                         prefix='.tokenforge-', suffix='.tmp', delete=False) as fp:
            temporary = Path(fp.name)
            writer(fp)
        if before_publish is not None:
            before_publish()
        if expected_destination is not None:
            warnings.extend(_replace_preserving_displaced(temporary, path, expected_destination))
            created = True
        elif replace:
            os.replace(temporary, path)
            created = True
        else:
            created = _publish_new(temporary, path)
        if created and after_publish is not None:
            try:
                after_publish()
            except OSError as exc:
                warnings.append(f'Результат записан, но исходник изменился или недоступен: {exc}. '
                                + late_conflict_hint)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                if failure is not None:
                    failure.add_note(f'Не удалось очистить временный файл {temporary}: {exc}')
                elif not created:
                    raise
                else:
                    warnings.append(f'Не удалось очистить временный файл {temporary}: {exc}')
    return Publication(created, tuple(warnings))


def save_token(image: Image.Image, path: Path, format_name: str = 'WebP',
               *, replace: bool = True) -> bool:
    if format_name.lower() == 'png':
        return bool(atomic_write(path, lambda fp: image.save(fp, 'PNG', optimize=True), replace=replace))
    return bool(atomic_write(path, lambda fp: image.save(fp, 'WEBP', lossless=True, method=6),
                             replace=replace))


def canonical_placement(adjustment):
    return {name: float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
            for name, value in asdict(adjustment).items()}


def recipe_digest(kind, **settings):
    payload = json.dumps({'kind': kind, 'algorithm': 1, **settings}, sort_keys=True,
                         ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('ascii')).hexdigest()


COPY_RECIPE = recipe_digest('copy', quality=95, method=6, alpha_quality=100,
                            color='sRGB', decoder=2)


def extraction_provenance(model, keep_alpha, edge_mode, *, revision=None):
    """Use the exact observed revision for rendered pixels, current one for freshness."""
    return (model, keep_alpha, edge_mode, CUTOUT_CACHE_VERSION,
            model_revision(model) if revision is None else revision)


def token_recipe(frame, adjustment, size, format_name, extraction, *, frame_digest=None):
    digest = frame_digest if frame_digest is not None else hashlib.sha256(frame.tobytes()).hexdigest()
    return recipe_digest('token', frame=digest, frame_size=frame.size, size=size,
                         format=format_name.lower(), placement=canonical_placement(adjustment),
                         extraction=extraction)


def _valid_digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def export_owner(source: Path, kind: str, *, source_digest=None, recipe=None) -> str:
    if source_digest is None:
        return 'TokenForge/2:' + json.dumps([kind, source.name], ensure_ascii=True)
    if not _valid_digest(source_digest) or (recipe is not None and not _valid_digest(recipe)):
        raise ValueError('Invalid export provenance')
    return 'TokenForge/3:' + json.dumps([kind, source.name, source_digest, recipe], ensure_ascii=True)


def freshness_warnings(owner, identity, recipe=None):
    """Ownership authorizes replacement; freshness never grants permission."""
    if identity is None:
        return ()
    if owner[0] < 3:
        return ('Актуальность старого результата не подтверждена; при необходимости создай его заново.',)
    messages = []
    if owner[3] != identity[-1]:
        messages.append('Исходник изменился; готовый результат устарел.')
    if recipe is None:
        messages.append('Не удалось проверить актуальность параметров генерации.')
    elif owner[4] != recipe:
        messages.append('Параметры генерации изменились; готовый результат может быть устаревшим.')
    if messages:
        messages.append('Существующий файл оставлен без изменений.')
    return tuple(messages)


class InvalidExportError(OSError):
    pass


def _parse_owner(owner):
    if not isinstance(owner, str) or len(owner) > 4096:
        return None
    prefix, separator, payload = owner.partition(':')
    version = {'TokenForge/1': 1, 'TokenForge/2': 2, 'TokenForge/3': 3}.get(prefix)
    if not separator or version is None:
        return None
    try:
        values = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if (isinstance(values, list) and len(values) == (4 if version == 3 else 2)
            and values[0] in ('token', 'item', 'copy')
            and isinstance(values[1], str) and values[1]
            and not any(c in values[1] for c in ('/', '\\', ':', '\0'))
            and values[1] not in ('.', '..')):
        if version == 3:
            if not _valid_digest(values[2]) or (values[3] is not None and not _valid_digest(values[3])):
                return None
            return version, *values
        return version, values[0], values[1]
    return None


def _probe_owner(stream, *, require_pixels=False):
    """Discovery is not evidence that the pixels are decodable."""
    try:
        with open_art(stream, formats=('PNG', 'WEBP')) as stored:
            owner = _parse_owner(stored.getexif().get(270))
            if owner and require_pixels:
                try:
                    stored.load()
                except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
                    raise InvalidExportError('Готовое изображение повреждено.') from exc
            return owner
    except InvalidExportError:
        raise
    except (OSError, ValueError, SyntaxError, TypeError, RecursionError, Image.DecompressionBombError):
        return None


def _probe_webp_owner(stream, size):
    """Read only bounded RIFF headers and EXIF, never create a WebP decoder.

    Discovery can recognize old exports even if current pixel limits would
    reject them. This is not pixel validation or authorization to replace.
    """
    header = stream.read(12)
    if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WEBP':
        return None
    end = 8 + struct.unpack_from('<I', header, 4)[0]
    if not 12 <= end <= size:
        return None
    owner = None
    image_chunk = False
    for index in range(4096):
        if stream.tell() == end:
            return owner if image_chunk else None
        if end - stream.tell() < 8:
            return None
        chunk = stream.read(8)
        if len(chunk) != 8:
            return None
        kind, length = struct.unpack('<4sI', chunk)
        next_chunk = stream.tell() + length + (length & 1)
        if next_chunk > end:
            return None
        if index == 0:
            # EXIF is only supported by extended WebP containers.
            if kind != b'VP8X' or length != 10:
                return None
            flags = stream.read(10)
            if len(flags) != 10 or not flags[0] & 0x08:
                return None
        elif kind == b'EXIF':
            # TokenForge ownership strings are <= 4096 characters. Allow
            # ample space for legacy EXIF, but never read an arbitrary chunk.
            if not 0 < length <= 1024 * 1024 or owner is not None:
                return None
            data = stream.read(length)
            if len(data) != length:
                return None
            exif = Image.Exif()
            exif.load(data)
            owner = _parse_owner(exif.get(270))
            if owner is None:
                return None
        elif kind in (b'VP8 ', b'VP8L', b'ANMF') and length:
            image_chunk = True
        stream.seek(next_chunk)
    return None


def read_export_owner(destination: Path, *, identity=None, require_pixels=False) -> tuple | None:
    """For mutations, inspect the exact bytes that were fingerprinted."""
    if destination.suffix.lower() not in ('.png', '.webp'):
        return None
    if identity is not None:
        with source_snapshot(destination, identity) as snapshot:
            return _probe_owner(snapshot, require_pixels=require_pixels)
    try:
        with destination.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_ENCODED_BYTES:
                return None
            header = stream.read(12)
            stream.seek(0)
            if not require_pixels and header[:4] == b'RIFF' and header[8:] == b'WEBP':
                return _probe_webp_owner(stream, size)
            return _probe_owner(stream, require_pixels=require_pixels)
    except InvalidExportError:
        raise
    except (OSError, ValueError, SyntaxError, TypeError, RecursionError, struct.error,
            IndexError, OverflowError, Image.DecompressionBombError):
        return None


def recorded_source_matches(source: Path, filename: str) -> bool:
    # Exact spelling is always valid; a case variant only if the filesystem
    # confirms that both names refer to the same existing file.
    if source.name == filename:
        return True
    if source.name.casefold() != filename.casefold():
        return False
    try:
        return source.samefile(source.with_name(filename))
    except OSError:
        return False


def is_owned_export(destination: Path, source: Path, kind: str, *, identity=None,
                    require_pixels=False) -> bool:
    owner = read_export_owner(destination, identity=identity, require_pixels=require_pixels)
    return _owner_matches(owner, source, kind)


def _owner_matches(owner, source, kind):
    if not owner or owner[1] != kind:
        return False
    if owner[0] >= 2:
        return recorded_source_matches(source, owner[2])
    if owner[2].casefold() != source.stem.casefold():
        return False
    # Old v1 markers lack an extension; resolve only when unambiguous.
    return not any(p.name != source.name and not recorded_source_matches(source, p.name)
                   and read_export_owner(p) is None
                   for p in live_stem_candidates(source.parent, {source.stem}))


def inspect_existing_export(destination, source, kind):
    """Return the destination's exact fingerprint and ownership from one read."""
    with fingerprinted_snapshot(destination) as (identity, snapshot):
        owner = _probe_owner(snapshot) if destination.suffix.lower() in ('.png', '.webp') else None
        return identity, _owner_matches(owner, source, kind)


def read_owned_export_image(destination, source, kind, *, include_owner=False):
    """Decode only the same verified bytes whose ownership was inspected."""
    with fingerprinted_snapshot(destination) as (identity, snapshot):
        with open_art(snapshot, formats=('PNG', 'WEBP')) as stored:
            owner = _parse_owner(stored.getexif().get(270))
            if not _owner_matches(owner, source, kind):
                return None
            image = stored.convert('RGBA')
    assert_source_identity(destination, identity)
    return (image, owner) if include_owner else image


def _windows_stem_names(folder, stem):
    """Ask Windows for matching names rather than enumerating the directory."""
    import ctypes
    from ctypes import wintypes

    class FindData(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('created', wintypes.FILETIME),
                    ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
                    ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD),
                    ('reserved0', wintypes.DWORD), ('reserved1', wintypes.DWORD),
                    ('name', wintypes.WCHAR * 260), ('alternate', wintypes.WCHAR * 14)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    first, next_file, close = api.FindFirstFileW, api.FindNextFileW, api.FindClose
    first.argtypes, first.restype = [wintypes.LPCWSTR, ctypes.POINTER(FindData)], wintypes.HANDLE
    next_file.argtypes, next_file.restype = [wintypes.HANDLE, ctypes.POINTER(FindData)], wintypes.BOOL
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    data = FindData()
    handle = first(windows_extended_path(folder / (stem + '.*')), ctypes.byref(data))
    if handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        if code in (2, 18):
            return
        raise ctypes.WinError(code)
    try:
        while True:
            if not data.attributes & 0x10:
                yield folder / data.name
            if not next_file(handle, ctypes.byref(data)):
                code = ctypes.get_last_error()
                if code != 18:
                    raise ctypes.WinError(code)
                break
    finally:
        close(handle)


def live_stem_candidates(folder, stems):
    folded = {stem.casefold() for stem in stems}
    if os.name == 'nt':
        # Native lookup follows Windows filesystem name matching, including Cyrillic.
        # Filter out DOS 8.3 aliases and unrelated wildcard matches.
        upper = {stem.upper() for stem in stems}
        candidates = (p for stem in stems for p in _windows_stem_names(folder, stem))
    else:
        # POSIX supplies no portable case-insensitive targeted directory search.
        upper = set()
        candidates = iter(list_images(folder))
    seen = set()
    for path in candidates:
        if (path.name not in seen and path.suffix.lower() in IMAGE_EXTS
                and (path.stem.casefold() in folded or path.stem.upper() in upper)
                and path.is_file()):
            seen.add(path.name)
            yield path


class FolderIndex:
    """One initial name index per batch; live publication guards are separate."""
    def __init__(self):
        self.folders = {}

    def candidates(self, folder, stems, *, live=False):
        if folder not in self.folders:
            indexed = {}
            with os.scandir(folder) as entries:
                for entry in entries:
                    path = Path(entry.name)
                    if path.suffix.lower() in IMAGE_EXTS:
                        indexed.setdefault(path.stem.casefold(), []).append(folder / entry.name)
            self.folders[folder] = indexed
        indexed = self.folders[folder]
        known = (p for stem in {s.casefold() for s in stems}
                 for p in indexed.get(stem, ()) if p.is_file())
        if not live:
            return known
        # Include known Unicode casefold variants even where native collation
        # has different rules; newly introduced names use native matching.
        from itertools import chain
        def unique():
            seen = set()
            for p in chain(known, live_stem_candidates(folder, stems)):
                if p.name not in seen:
                    seen.add(p.name)
                    yield p
        return unique()


def ensure_no_source_collision(destination: Path, source: Path, kind: str, index=None, *, live=False) -> None:
    index = index if index is not None else FolderIndex()
    stems = {source.stem}
    if destination.suffix.lower() == '.webp':
        stems.add(destination.stem)
    for candidate in index.candidates(source.parent, stems, live=live):
        if candidate.name in (source.name, destination.name):
            continue
        try:
            if candidate.samefile(source) or (destination.exists() and candidate.samefile(destination)):
                continue
        except OSError:
            pass
        if read_export_owner(candidate) is None:
            raise FileExistsError(f'Имя {destination.name} конфликтует с исходником {candidate.name}. '
                                  'Переименуй один из исходников.')


def validate_skip(destination, source, kind, *, identity=None):
    identity = identity if identity is not None else source_identity(destination)
    try:
        owner = read_export_owner(destination, identity=identity, require_pixels=True)
        owned = _owner_matches(owner, source, kind)
    except InvalidExportError as exc:
        hint = ('Перемести или удали повреждённую WebP-копию и повтори обработку.'
                if kind == 'copy' else 'Отключи пропуск готовых изображений и повтори обработку.')
        raise InvalidExportError(f'{destination.name}: {exc} {hint}') from exc
    if not owned:
        hint = ' Перемести или переименуй существующий файл и повтори обработку.' if kind == 'copy' else ''
        raise FileExistsError(f'Имя {destination.name} занято неподтверждённым изображением.{hint}')
    assert_source_identity(destination, identity)
    return owner


def save_generated(image: Image.Image, destination: Path, source: Path, kind: str,
                   *, skip_exists: bool, approved_identity: tuple | None = None,
                   input_identity: tuple | None = None, collision_index=None, recipe=None,
                   skip_recipe=None) -> Publication:
    # A late competing publication must be compared with the current settings,
    # not the actual extraction recipe of pixels that we did not publish.
    def skipped(owner):
        current = skip_recipe() if skip_recipe is not None else recipe
        return Publication(False, freshness_warnings(owner, input_identity, current))

    # A suffix is not proof of ownership. Old unmarked exports require explicit
    # confirmation; automatic and batch jobs may only overwrite marked results.
    prior = source_identity(destination) if destination.exists() else None
    if approved_identity is EXPECTED_ABSENT and prior is not None:
        raise FileExistsError(f'Файл {destination.name} появился после начала сохранения. Повтори сохранение.')
    if approved_identity is not None and approved_identity is not EXPECTED_ABSENT and prior != approved_identity:
        raise FileExistsError(f'Файл {destination.name} изменился после подтверждения замены. Подтверди заново.')
    if prior is not None:
        owned = is_owned_export(destination, source, kind, identity=prior)
        if skip_exists and owned:
            if collision_index is not None:
                ensure_no_source_collision(destination, source, kind, collision_index, live=True)
            owner = validate_skip(destination, source, kind, identity=prior)
            if input_identity is not None:
                assert_source_identity(source, input_identity)
            return skipped(owner)
        if not owned and prior != approved_identity:
            raise FileExistsError(f'Файл {destination.name} не подтверждён как результат TokenForge. '
                                  'Переименуй его или подтверди замену вручную.')
    exif = Image.Exif()
    exif[270] = export_owner(source, kind,
                             source_digest=input_identity[-1] if input_identity is not None else None,
                             recipe=recipe)
    if destination.suffix.lower() not in ('.png', '.webp'):
        raise ValueError('Результат TokenForge должен иметь расширение PNG или WebP.')
    fmt = 'PNG' if destination.suffix.lower() == '.png' else 'WEBP'
    options = {'optimize': True} if fmt == 'PNG' else {'lossless': True, 'method': 6}
    def encode(fp):
        image.save(fp, fmt, exif=exif, **options)
    def check_input():
        if input_identity is not None:
            assert_source_identity(source, input_identity)
    def guard():
        if collision_index is not None:
            ensure_no_source_collision(destination, source, kind, collision_index, live=True)
        if prior is not None:
            assert_source_identity(destination, prior)
        check_input()
    published = atomic_write(destination, encode, replace=False,
                             before_publish=guard, expected_destination=prior,
                             after_publish=check_input)
    if not published:
        if not skip_exists:
            raise FileExistsError(f'Файл появился во время экспорта: {destination.name}')
        if collision_index is not None:
            ensure_no_source_collision(destination, source, kind, collision_index, live=True)
        owner = validate_skip(destination, source, kind)
        check_input()
        return skipped(owner)
    return published


def list_images(folder: Path) -> list[Path]:
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
                  key=lambda p: p.name.casefold())


def original_webp_path(source: Path) -> Path:
    """Output artwork beside the original, keeping the input unchanged."""
    return source.with_suffix('.webp')


def token_output_path(source: Path, format_name: str = 'webp') -> Path:
    """Output beside source using the user's requested t suffix."""
    if not isinstance(format_name, str) or format_name.lower() not in ('png', 'webp'):
        raise ValueError('Неподдерживаемый формат токена.')
    return source.with_name(f'{source.stem}t.{format_name.lower()}')


EXPECTED_ABSENT = object()


class StaleApprovalError(OSError):
    """An explicit overwrite approval no longer matches its destination."""


def assert_approval(destination, approved):
    if approved is not None:
        try:
            if approved is EXPECTED_ABSENT:
                if file_stamp(destination) is not None:
                    raise FileExistsError(destination)
            else:
                assert_source_identity(destination, approved)
        except OSError as exc:
            raise StaleApprovalError(f'Файл {destination.name} изменился после подтверждения замены. Подтверди заново.') from exc


def save_original_webp(source: Path, *, collision_index=None, input_identity=None,
                       approval_guard=None, captured=None) -> Publication:
    """Create a high-quality copy, preserving alpha and image dimensions.

    Returns True only if a new file was written. Never overwrite an existing
    WebP (which might be a hand-edited asset).
    """
    identity = input_identity if input_identity is not None else source_identity(source)
    if captured is not None and (captured.path != source or captured.identity != identity):
        raise ValueError('Снимок не соответствует исходнику.')
    if approval_guard is not None:
        approval_guard()
    if source.suffix.lower() == '.webp':
        return Publication(False)
    destination = original_webp_path(source)
    if destination.exists():
        if collision_index is not None:
            ensure_no_source_collision(destination, source, 'copy', collision_index, live=True)
        owner = validate_skip(destination, source, 'copy')
        assert_source_identity(source, identity)
        return Publication(False, freshness_warnings(owner, identity, COPY_RECIPE))
    if collision_index is not None:
        ensure_no_source_collision(destination, source, 'copy', collision_index)
    def guard():
        if collision_index is not None:
            ensure_no_source_collision(destination, source, 'copy', collision_index, live=True)
        assert_source_identity(source, identity)
        if approval_guard is not None:
            approval_guard()
    capture_context = CapturedArtwork(source, identity) if captured is None else nullcontext(captured)
    with capture_context as capture, capture.open() as raw:
        artwork = decode_artwork(raw)
        color_options = {'icc_profile': artwork.info['icc_profile']} if artwork.info.get('icc_profile') else {}
        exif = Image.Exif()
        exif[270] = export_owner(source, 'copy', source_digest=identity[-1], recipe=COPY_RECIPE)
        created = atomic_write(destination,
                               lambda fp: artwork.save(fp, format='WEBP', quality=95,
                                                       method=6, alpha_quality=100, exif=exif,
                                                       **color_options),
                               replace=False, before_publish=guard,
                               after_publish=lambda: assert_source_identity(source, identity),
                               late_conflict_hint='Перемести или удали устаревшую WebP-копию и повтори обработку.')
        if not created:
            if collision_index is not None:
                ensure_no_source_collision(destination, source, 'copy', collision_index, live=True)
            owner = validate_skip(destination, source, 'copy')
            assert_source_identity(source, identity)
            return Publication(False, freshness_warnings(owner, identity, COPY_RECIPE))
        return created


def excluded_file(path: Path, excluded: Path | None) -> bool:
    if excluded is None:
        return False
    try:
        return path.samefile(excluded)
    except OSError:
        return False


def list_source_images(folder: Path, exclude: Path | None = None, *, cancel_event=None) -> list[Path]:
    """Exclude the selected frame (including aliases) and validated results."""
    result = []
    for path in list_images(folder):
        if cancel_event is not None and cancel_event.is_set():
            break
        if excluded_file(path, exclude):
            continue
        if read_export_owner(path) is None:
            result.append(path)
    return result


@dataclass(frozen=True)
class LoadedCutout:
    image: Image.Image
    identity: tuple
    extraction: tuple | None = None


@dataclass
class BatchResult:
    created: int = 0
    skipped: int = 0
    converted: int = 0
    errors: list[str] | None = None
    cancelled: bool = False
    total: int = 0
    error_details: list[str] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record_error(self, source: Path, stage: str, exc: Exception) -> None:
        message = f'{source.name}: {stage}: {exc}'
        if getattr(exc, '__notes__', None):
            message += '\n' + '\n'.join(exc.__notes__)
        self.errors.append(message)
        self.error_details.append(message + '\n' + traceback.format_exc())


def export_images(files, get_cutout, output_path, renderer, *, kind,
                  skip_exists=True, convert_original=True, cancel_event=None,
                  on_progress=None, approved_outputs=None, expected_inputs=None,
                  recipe_for=None, captured_loader=None) -> BatchResult:
    """One export pipeline for both composition modes.

    Capture the source version before invoking the loader and carry it
    through composition and publication.
    """
    result = BatchResult(errors=[], total=len(files))
    stage = 'генерация токена' if kind == 'token' else 'генерация предмета'
    folders = FolderIndex()
    for index, source in enumerate(files):
        if cancel_event is not None and cancel_event.is_set():
            result.cancelled = True
            break
        if on_progress:
            on_progress(index, len(files), f'[{index + 1}/{len(files)}] {source.name}')
        try:
            destination = output_path(source)
            approved = (approved_outputs or {}).get(destination)
            identity = source_identity(source)
            expected = (expected_inputs or {}).get(str(source))
            if expected is not None and expected != identity:
                raise OSError(f'Исходный файл изменился во время обработки: {source.name}')
            # A stale approval must not trigger either of this job's writes.
            assert_approval(destination, approved)
        except Exception as exc:
            result.record_error(source, stage, exc)
            if on_progress:
                on_progress(index+1, len(files), f'Выполнено: {index + 1}/{len(files)}')
            continue
        with CapturedArtwork(source, identity) as captured:
            if convert_original:
                try:
                    publication = save_original_webp(
                        source, collision_index=folders, input_identity=identity, captured=captured,
                        approval_guard=(lambda: assert_approval(destination, approved)) if approved is not None else None)
                    result.converted += int(bool(publication))
                    copy_destination = original_webp_path(source)
                    result.warnings.extend(f'{source.name}: {copy_destination.name}: {warning}'
                                           for warning in publication.warnings)
                except StaleApprovalError as exc:
                    result.record_error(source, 'преобразование оригинала', exc)
                    if on_progress:
                        on_progress(index + 1, len(files), f'Выполнено: {index + 1}/{len(files)}')
                    continue
                except Exception as exc:
                    result.record_error(source, 'преобразование оригинала', exc)
            try:
                assert_approval(destination, approved)
                ensure_no_source_collision(destination, source, kind, folders)
                if skip_exists and destination.exists():
                    ensure_no_source_collision(destination, source, kind, folders, live=True)
                    owner = validate_skip(destination, source, kind)
                    assert_source_identity(source, identity)
                    recipe = recipe_for(source) if recipe_for is not None else None
                    result.warnings.extend(f'{source.name}: {destination.name}: {w}'
                                           for w in freshness_warnings(owner, identity, recipe))
                    result.skipped += 1
                else:
                    recipe = recipe_for(source) if recipe_for is not None else None
                    if captured_loader is None:
                        # Legacy injectable loader contract retains the old guard.
                        assert_source_identity(source, identity)
                        loaded = get_cutout(source)
                        assert_source_identity(source, identity)
                    else:
                        loaded = captured_loader(captured)
                    if isinstance(loaded, LoadedCutout):
                        if loaded.identity != identity:
                            raise OSError(f'Исходный файл изменился во время обработки: {source.name}')
                        cutout = loaded.image
                        identity = loaded.identity
                    else:
                        cutout = loaded
                    image = renderer(cutout, source)
                    # Use the revision that actually produced the pixels, not
                    # an observation made before first download or model reload.
                    actual = loaded.extraction if isinstance(loaded, LoadedCutout) else None
                    if recipe_for is not None and (recipe is None or actual is not None):
                        recipe = recipe_for(source, actual)
                    publication = save_generated(image, destination, source, kind, skip_exists=skip_exists,
                                                 approved_identity=approved,
                                                 input_identity=identity, collision_index=folders, recipe=recipe,
                                                 skip_recipe=(lambda: recipe_for(source)) if recipe_for is not None else None)
                    result.warnings.extend(f'{source.name}: {destination.name}: {warning}' for warning in publication.warnings)
                    if publication:
                        result.created += 1
                        result.outputs.append(destination)
                    else:
                        result.skipped += 1
            except Exception as exc:
                result.record_error(source, stage, exc)
        if on_progress:
            on_progress(index + 1, len(files), f'Выполнено: {index + 1}/{len(files)}')
    return result


def export_batch(files: list[Path], get_cutout, frame: Image.Image | None,
                 interior: Image.Image | None, adjustments: dict[Path, Adjust],
                 out_size: int = 256, format_name: str = 'webp',
                 skip_exists: bool = True, convert_original: bool = True,
                 cancel_event=None, on_progress=None, approved_outputs=None,
                 frame_loader=None, expected_inputs=None, extraction_recipe=None,
                 captured_loader=None) -> BatchResult:
    frame_digest = None
    prepared_frame = None
    def ensure_frame():
        nonlocal frame, interior
        if frame is None and frame_loader is not None:
            frame, interior = frame_loader()
        return frame
    def recipe_for(source, actual=None):
        nonlocal frame_digest
        try:
            if ensure_frame() is None:
                return None
        except (OSError, ValueError, Image.DecompressionBombError):
            # Preserve the ability to skip existing valid pixels without an
            # available frame; rendering a new result still requires it.
            return None
        if frame_digest is None:
            frame_digest = hashlib.sha256(frame.tobytes()).hexdigest()
        extraction = actual if actual is not None else (
            extraction_recipe() if callable(extraction_recipe) else extraction_recipe)
        return token_recipe(frame, adjustments[source], out_size, format_name, extraction,
                            frame_digest=frame_digest)
    def render(cutout, source):
        nonlocal prepared_frame
        if ensure_frame() is None or interior is None:
            raise ValueError('Выбери корректное PNG-кольцо.')
        if prepared_frame is None:
            prepared_frame = prepare_token_frame(frame, interior, out_size)
        return compose(cutout, frame, interior, out_size, adjustments[source],
                       prepared_frame=prepared_frame)
    return export_images(files, get_cutout, lambda p: token_output_path(p, format_name),
                         render, kind='token', skip_exists=skip_exists,
                         convert_original=convert_original, cancel_event=cancel_event,
                         on_progress=on_progress, approved_outputs=approved_outputs, expected_inputs=expected_inputs,
                         recipe_for=recipe_for, captured_loader=captured_loader)
