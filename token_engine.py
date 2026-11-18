"""TokenForge image pipeline. Tk-independent so composition can be tested offline."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
import tempfile
import traceback
from pathlib import Path
from statistics import median
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageChops

CUTOUT_CACHE_VERSION = 2

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}


@dataclass
class Adjust:
    zoom: float = 100.0       # percent of automatically selected zoom
    x: float = 0.0            # token-width percent
    y: float = 0.0            # token-height percent
    gap: float = 11.0         # head-to-top gap, token-height percent


def source_identity(source: Path) -> tuple:
    resolved = Path(source).resolve()
    def signature(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with resolved.open('rb') as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if signature(before) != signature(after) or signature(after) != signature(resolved.stat()):
        raise OSError(f'Исходный файл изменился во время обработки: {source.name}')
    return str(resolved), after.st_size, after.st_mtime_ns, digest


def assert_source_identity(source: Path, identity: tuple) -> None:
    if source_identity(source) != identity:
        raise OSError(f'Исходный файл изменился во время обработки: {source.name}')


class DiskCutoutCache:
    """Lossless persistent cache. No image extraction rerun for unchanged inputs."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path_for(self, source: Path, model: str, keep_alpha: bool, edge_mode: str,
                 *, identity: tuple | None = None) -> Path:
        version = identity or source_identity(source)
        key = [CUTOUT_CACHE_VERSION, version[0], version[-1],
               model, bool(keep_alpha), edge_mode]
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode('utf-8')).hexdigest()
        return self.directory / f'{digest}.png'

    def load(self, source: Path, model: str, keep_alpha: bool, edge_mode: str,
             *, identity: tuple | None = None) -> Image.Image | None:
        path = self.path_for(source, model, keep_alpha, edge_mode, identity=identity)
        try:
            with Image.open(path) as stored:
                stored.load()
                if stored.format != 'PNG':
                    raise ValueError('Invalid cache format')
                return stored.convert('RGBA')
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # A read-only cache must not block fresh extraction.
            return None

    def save(self, source: Path, model: str, keep_alpha: bool,
             edge_mode: str, cutout: Image.Image, *, identity: tuple | None = None) -> None:
        destination = self.path_for(source, model, keep_alpha, edge_mode, identity=identity)
        if identity is not None:
            assert_source_identity(source, identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', dir=destination.parent,
                                             prefix='.tmp-', delete=False) as tmp:
                temporary = Path(tmp.name)
                cutout.convert('RGBA').save(tmp, format='PNG', compress_level=1)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def read_image(path: Path) -> Image.Image:
    with Image.open(path) as img:
        result = ImageOps.exif_transpose(img).convert('RGBA')
    return result


def already_transparent(img: Image.Image) -> bool:
    # Check real alpha; RGBA JPEG conversions have a fully opaque alpha channel.
    return img.getchannel('A').getextrema()[0] < 250


def remove_background(img: Image.Image, session, edge_mode: str = 'Очистка краёв') -> Image.Image:
    from rembg import remove
    # Never hard-threshold the soft mask; fine hair depends on partial alpha.
    kwargs = {'session': session}
    if edge_mode == 'Очистка краёв':
        kwargs['decontaminate'] = True
    elif edge_mode == 'Alpha matting (медленно)':
        kwargs['alpha_matting'] = True
    elif edge_mode != 'Обычный':
        raise ValueError(f'Неизвестный режим краёв: {edge_mode}')
    result = remove(img.convert('RGB'), **kwargs)
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
            step = min(2 * covered + 1, radius - covered)
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

    outside = barrier.copy()
    # Fill every exterior component touching an edge, even when a corner is opaque.
    for x in range(frame.width):
        for y in (0, frame.height - 1):
            if outside.getpixel((x, y)) == 0:
                ImageDraw.floodfill(outside, (x, y), 255, thresh=0)
    for y in range(frame.height):
        for x in (0, frame.width - 1):
            if outside.getpixel((x, y)) == 0:
                ImageDraw.floodfill(outside, (x, y), 255, thresh=0)
    outer_shape = outside.point(lambda p: 0 if p == 255 else 255)

    thickness = _ring_thickness(alpha)
    under_ring = max(1, int(round(thickness * 0.5)))
    clip_mask = square_dilate(inside, under_ring)
    clip_mask = ImageChops.multiply(clip_mask, outer_shape)
    return clip_mask.filter(ImageFilter.GaussianBlur(.6))


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


def compose(cutout: Image.Image, frame: Image.Image, interior: Image.Image,
            out_size: int = 256, adjust: Adjust | None = None) -> Image.Image:
    """Place cutout against a ring, clipping everything outside its interior."""
    if adjust is None:
        adjust = Adjust()
    center_x, head_width, top_y = silhouette_geometry(cutout)
    # Typical tabletop portrait has hair/head ~40% of token width.
    base_scale = .40 * out_size / max(head_width, 1)
    scale = max(0.005, base_scale * adjust.zoom / 100)
    ox = out_size * .5 - center_x * scale + adjust.x / 100 * out_size
    oy = out_size * adjust.gap / 100 - top_y * scale + adjust.y / 100 * out_size

    # Crop at source resolution first so huge source images never create a
    # massive intermediate image when the automatic zoom is high.
    import math
    left = math.floor(-ox / scale) - 2
    upper = math.floor(-oy / scale) - 2
    right = math.ceil((out_size - ox) / scale) + 2
    lower = math.ceil((out_size - oy) / scale) + 2
    left, upper = max(0, left), max(0, upper)
    right, lower = min(cutout.width, right), min(cutout.height, lower)
    image_layer = Image.new('RGBA', (out_size, out_size), (0, 0, 0, 0))
    if right > left and lower > upper:
        cropped = cutout.crop((left, upper, right, lower))
        target_size = (max(1, int(round((right - left) * scale))),
                       max(1, int(round((lower - upper) * scale))))
        scaled = cropped.resize(target_size, Image.Resampling.LANCZOS)
        image_layer.paste(scaled, (int(round(ox + left * scale)), int(round(oy + upper * scale))))
    inner = interior.resize((out_size, out_size), Image.Resampling.LANCZOS)
    image_layer.putalpha(ImageChops.multiply(image_layer.getchannel('A'), inner))
    token = Image.alpha_composite(image_layer, frame.resize((out_size, out_size), Image.Resampling.LANCZOS))
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


def _replace_preserving_displaced(temporary: Path, destination: Path, expected: tuple) -> None:
    """Never silently discard an externally replaced file.

    ReplaceFileW publishes atomically and saves the actual displaced file.
    On POSIX use a recovery move followed by no-clobber publication.
    """
    import uuid
    recovery = destination.parent / '.tokenforge-recovery'
    recovery.mkdir(exist_ok=True)
    backup = recovery / f'{uuid.uuid4().hex}-{destination.name}'
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        replace = ctypes.WinDLL('kernel32', use_last_error=True).ReplaceFileW
        replace.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
        replace.restype = wintypes.BOOL
        if not replace(str(destination), str(temporary), str(backup), 0, None, None):
            raise OSError(f'Не удалось заменить {destination.name}; '
                          f'проверь также папку восстановления {recovery}: '
                          f'{ctypes.WinError(ctypes.get_last_error())}')
    else:
        os.rename(destination, backup)
        try:
            if not _publish_new(temporary, destination):
                raise FileExistsError(f'Файл появился во время экспорта: {destination.name}')
        except BaseException:
            # Restore only into a vacant name; never clobber another writer.
            if _publish_new(backup, destination):
                backup.unlink(missing_ok=True)
            raise
    # Compare the bytes of the actually displaced file, not the path checked
    # before publication. Retain any unexpected version for recovery.
    if source_identity(backup)[-1] != expected[-1]:
        raise FileExistsError(f'Результат записан, но другой редактор изменил файл. '
                              f'Вытесненная версия сохранена: {backup}')
    backup.unlink()
    # The recovery directory is intentionally kept; another writer may use it.


def atomic_write(path: Path, writer, *, replace: bool = True, before_publish=None,
                 expected_destination=None) -> bool:
    """Publish only a complete, closed file, optionally without replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent,
                                         prefix='.tokenforge-', suffix='.tmp', delete=False) as fp:
            temporary = Path(fp.name)
            writer(fp)
        if before_publish is not None:
            before_publish()
        if expected_destination is not None:
            _replace_preserving_displaced(temporary, path, expected_destination)
        elif replace:
            os.replace(temporary, path)
        else:
            return _publish_new(temporary, path)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_token(image: Image.Image, path: Path, format_name: str = 'WebP',
               *, replace: bool = True) -> bool:
    if format_name.lower() == 'png':
        return atomic_write(path, lambda fp: image.save(fp, 'PNG', optimize=True), replace=replace)
    return atomic_write(path, lambda fp: image.save(fp, 'WEBP', lossless=True, method=6),
                        replace=replace)


def export_owner(source: Path, kind: str) -> str:
    return 'TokenForge/2:' + json.dumps([kind, source.name], ensure_ascii=True)


def read_export_owner(destination: Path) -> tuple | None:
    """Read a validated marker; arbitrary filename suffixes are never proof."""
    try:
        with Image.open(destination) as stored:
            owner = stored.getexif().get(270)
        if not isinstance(owner, str):
            return None
        prefix, payload = owner.split(':', 1)
        version = {'TokenForge/1': 1, 'TokenForge/2': 2}.get(prefix)
        values = json.loads(payload)
        if (version and isinstance(values, list) and len(values) == 2
                and values[0] in ('token', 'item', 'copy')
                and isinstance(values[1], str) and values[1]
                and '/' not in values[1] and '\\' not in values[1]
                and values[1] not in ('.', '..')):
            return version, values[0], values[1]
    except (OSError, ValueError, SyntaxError, TypeError):
        pass
    return None


def is_owned_export(destination: Path, source: Path, kind: str) -> bool:
    owner = read_export_owner(destination)
    if not owner or owner[1] != kind:
        return False
    return owner[2].casefold() == (source.name if owner[0] == 2 else source.stem).casefold()


def ensure_no_source_collision(destination: Path, source: Path, kind: str) -> None:
    # Two independent originals with the same stem have identical output names.
    # A different original may also need destination as its own WebP copy.
    for candidate in list_source_images(source.parent):
        if candidate == source:
            continue
        if candidate.resolve() == destination.resolve():
            # An unmarked exact-path conflict is handled by save_generated,
            # including the user's explicit approval of a legacy export.
            continue
        if (candidate.stem.casefold() == source.stem.casefold()
                or (destination.suffix.lower() == '.webp'
                    and candidate.stem.casefold() == destination.stem.casefold())):
            raise FileExistsError(f'Имя {destination.name} конфликтует с исходником {candidate.name}. '
                                  'Переименуй один из исходников.')


def save_generated(image: Image.Image, destination: Path, source: Path, kind: str,
                   *, skip_exists: bool, approved_identity: tuple | None = None,
                   input_identity: tuple | None = None) -> bool:
    # A suffix is not proof of ownership. Old unmarked exports require explicit
    # confirmation; automatic and batch jobs may only overwrite marked results.
    prior = source_identity(destination) if destination.exists() else None
    if prior is not None:
        owned = is_owned_export(destination, source, kind)
        if skip_exists and owned:
            return False
        if not owned and prior != approved_identity:
            raise FileExistsError(f'Файл {destination.name} не подтверждён как результат TokenForge. '
                                  'Переименуй его или подтверди замену вручную.')
    exif = Image.Exif()
    exif[270] = export_owner(source, kind)
    fmt = 'PNG' if destination.suffix.lower() == '.png' else 'WEBP'
    options = {'optimize': True} if fmt == 'PNG' else {'lossless': True, 'method': 6}
    def encode(fp):
        image.save(fp, fmt, exif=exif, **options)
    def guard():
        if input_identity is not None:
            assert_source_identity(source, input_identity)
        if prior is not None:
            assert_source_identity(destination, prior)
    published = atomic_write(destination, encode, replace=False,
                             before_publish=guard, expected_destination=prior)
    if not published and (not skip_exists or not is_owned_export(destination, source, kind)):
        raise FileExistsError(f'Файл появился во время экспорта: {destination.name}')
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
    return source.with_name(f'{source.stem}t.{format_name.lower()}')


def save_original_webp(source: Path) -> bool:
    """Create a high-quality copy, preserving alpha and image dimensions.

    Returns True only if a new file was written. Never overwrite an existing
    WebP (which might be a hand-edited asset).
    """
    if source.suffix.lower() == '.webp':
        return False
    destination = original_webp_path(source)
    if destination.exists():
        if not is_owned_export(destination, source, 'copy'):
            raise FileExistsError(f'Имя WebP-копии {destination.name} занято другим изображением.')
        return False
    identity = source_identity(source)
    with Image.open(source) as raw:
        artwork = ImageOps.exif_transpose(raw)
        # Preserve true transparency without accidentally adding an opaque alpha.
        if 'A' in artwork.getbands() or 'transparency' in artwork.info:
            artwork = artwork.convert('RGBA')
        else:
            artwork = artwork.convert('RGB')
        exif = Image.Exif()
        exif[270] = export_owner(source, 'copy')
        created = atomic_write(destination,
                               lambda fp: artwork.save(fp, format='WEBP', quality=95,
                                                       method=6, alpha_quality=100, exif=exif),
                               replace=False, before_publish=lambda: assert_source_identity(source, identity))
        if not created and not is_owned_export(destination, source, 'copy'):
            raise FileExistsError(f'Имя WebP-копии {destination.name} занято другим изображением.')
        return created


def list_source_images(folder: Path, exclude: Path | None = None) -> list[Path]:
    """Only validated provenance excludes generated art, even if orphaned."""
    excluded = exclude.resolve() if exclude is not None else None
    return [path for path in list_images(folder)
            if (excluded is None or path.resolve() != excluded)
            and read_export_owner(path) is None]


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

    def record_error(self, source: Path, stage: str, exc: Exception) -> None:
        message = f'{source.name}: {stage}: {exc}'
        self.errors.append(message)
        self.error_details.append(message + '\n' + traceback.format_exc())


def export_images(files, get_cutout, output_path, renderer, *, kind,
                  skip_exists=True, convert_original=True, cancel_event=None,
                  on_progress=None, approved_outputs=None) -> BatchResult:
    """One export pipeline for both composition modes.

    Capture the source version before invoking the loader and carry it
    through composition and publication.
    """
    result = BatchResult(errors=[], total=len(files))
    stage = 'генерация токена' if kind == 'token' else 'генерация предмета'
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
        destination = output_path(source)
        try:
            ensure_no_source_collision(destination, source, kind)
            if skip_exists and destination.exists():
                if not is_owned_export(destination, source, kind):
                    raise FileExistsError(f'Имя {destination.name} занято неподтверждённым изображением.')
                result.skipped += 1
            else:
                identity = source_identity(source)
                cutout = get_cutout(source)
                assert_source_identity(source, identity)
                image = renderer(cutout, source)
                if save_generated(image, destination, source, kind, skip_exists=skip_exists,
                                  approved_identity=(approved_outputs or {}).get(destination),
                                  input_identity=identity):
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
                 frame_loader=None) -> BatchResult:
    def render(cutout, source):
        nonlocal frame, interior
        if frame is None and frame_loader is not None:
            frame, interior = frame_loader()
        return compose(cutout, frame, interior, out_size, adjustments[source])
    return export_images(files, get_cutout, lambda p: token_output_path(p, format_name),
                         render, kind='token', skip_exists=skip_exists,
                         convert_original=convert_original, cancel_event=cancel_event,
                         on_progress=on_progress, approved_outputs=approved_outputs)
