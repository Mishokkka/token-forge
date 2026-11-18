"""Shared extraction/cache policy for GUI and Explorer/CLI entry points."""
from token_engine import already_transparent, assert_source_identity, validate_art_dimensions
from local_models import require_local_model, model_revision
from process_lock import process_lock

MEMORY_LIMIT_BYTES = 256 * 1024**2


def remember(memory, key, image, *, count_limit=6, byte_limit=MEMORY_LIMIT_BYTES):
    """Cache ownership is bounded by decoded pixel bytes, not just count."""
    memory.pop(key, None)
    weight = lambda im: im.width * im.height * 4
    if weight(image) <= byte_limit and count_limit > 0:
        memory[key] = image
    while len(memory) > count_limit or sum(weight(im) for im in memory.values()) > byte_limit:
        memory.pop(next(iter(memory)))


def load_cutout(path, model, keep_alpha, edge_mode, *, key, disk_cache, memory, sessions,
                decode, extract, session_factory, cached_only=False, memory_limit=6,
                verify=None):
    require_local_model(model)
    identity = key[:4]
    check = verify if verify is not None else lambda: assert_source_identity(path, identity)
    revision = model_revision(model)
    memory_key = (*key, revision)

    def cached():
        image = memory.get(memory_key)
        if image is not None:
            return image
        return disk_cache.load(path, model, keep_alpha, edge_mode, identity=identity, revision=revision)

    cutout = cached()
    new = cutout is None
    if new:
        if cached_only:
            check()
            return None
        original = decode(path, identity=identity)
        if keep_alpha and already_transparent(original):
            cutout = original
        else:
            # Cooperating automatic commands and GUI instances share one
            # inference slot. No lock is held for cache-only viewing.
            with process_lock(disk_cache.directory.parent / 'inference', timeout=120):
                revision = model_revision(model)
                memory_key = (*key, revision)
                cutout = cached()
                if cutout is None:
                    existing = sessions.get(model)
                    if existing is None or existing[0] != revision:
                        # Drop other ORT sessions before loading another model.
                        sessions.clear()
                        session = session_factory(model)
                        revision = model_revision(model)  # first download may change it
                        memory_key = (*key, revision)
                        sessions[model] = (revision, session)
                    else:
                        session = existing[1]
                    cutout = extract(original, session, edge_mode)
                    if model_revision(model) != revision:
                        sessions.clear()
                        raise OSError('Файлы модели изменились во время удаления фона. Повтори обработку.')
        validate_art_dimensions(cutout)
        # The cache writer checks the source after encoding, before publishing.
        # If disk caching fails, validate independently before returning pixels.
        try:
            disk_cache.save(path, model, keep_alpha, edge_mode, cutout,
                            identity=identity, revision=revision)
        except OSError:
            check()
    else:
        check()
    remember(memory, memory_key, cutout, count_limit=memory_limit)
    return cutout
