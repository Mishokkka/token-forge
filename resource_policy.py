"""Limits for operations, not just file formats.

These are conservative work-size budgets, not guarantees about process RSS.
Native models and third-party libraries also allocate their own memory.
"""
from dataclasses import dataclass

MIB = 1024 ** 2
MAX_ENCODED_BYTES = 256 * MIB
MAX_IMAGE_PIXELS = 64 * MIB
MAX_IMAGE_SIDE = 16384
MAX_RING_SIDE = 4096
TOKEN_OUTPUT_SIZES = (256, 512, 1024)
MAX_OUTPUT_SIDE = max(TOKEN_OUTPUT_SIZES)
DEFAULT_EDGE_MODE = 'Очистка краёв'
MAX_COMPOSITION_PIXELS = 16 * MIB
MEMORY_CACHE_BYTES = 256 * MIB
DISK_CACHE_BYTES = 2 * 1024 * MIB
DISK_CACHE_ENTRIES = 4096


@dataclass(frozen=True)
class ExtractionPolicy:
    max_pixels: int
    options: tuple = ()


# The cleanup kernels allocate multiple float64 arrays per pixel; alpha
# matting additionally builds sparse systems. Do not silently reduce quality,
# switch modes, or apply these limits to already-transparent/cache-hit art.
EXTRACTION_POLICIES = {
    DEFAULT_EDGE_MODE: ExtractionPolicy(4 * MIB, (('decontaminate', True),)),
    'Обычный': ExtractionPolicy(16 * MIB),
    'Alpha matting (медленно)': ExtractionPolicy(1 * MIB, (('alpha_matting', True),)),
}
EDGE_MODES = tuple(EXTRACTION_POLICIES)


def extraction_options(edge_mode):
    try:
        return EXTRACTION_POLICIES[edge_mode]
    except (KeyError, TypeError):
        raise ValueError(f'Неизвестный режим краёв: {edge_mode}') from None


def validate_extraction_dimensions(image, edge_mode):
    policy = extraction_options(edge_mode)
    if image.width * image.height > policy.max_pixels:
        raise ValueError(
            f'Режим «{edge_mode}» поддерживает до {policy.max_pixels // MIB} МиПикс. '
            f'Исходник {image.width} × {image.height} слишком велик. '
            'Уменьши разрешение или выбери менее ресурсоёмкий режим.')
    return policy


def validate_output_size(size):
    if type(size) is not int or not 1 <= size <= MAX_OUTPUT_SIDE:
        raise ValueError(f'Размер результата должен быть от 1 до {MAX_OUTPUT_SIDE} пикселей.')
    return size


def validate_intermediate_size(size):
    if min(size) < 1 or max(size) > MAX_IMAGE_SIDE or size[0] * size[1] > MAX_COMPOSITION_PIXELS:
        raise ValueError('Масштаб слишком велик для безопасной компоновки.')
    return size
