"""Shared extraction/cache policy for GUI and Explorer/CLI entry points."""
from token_engine import already_transparent, assert_source_identity
from local_models import require_local_model


def load_cutout(path, model, keep_alpha, edge_mode, *, key, disk_cache, memory, sessions,
                decode, extract, session_factory, cached_only=False, memory_limit=6):
    require_local_model(model)
    identity = key[:4]
    cutout = memory.get(key)
    if cutout is None:
        cutout = disk_cache.load(path, model, keep_alpha, edge_mode, identity=identity)
    if cutout is None:
        if cached_only:
            assert_source_identity(path, identity)
            return None
        original = decode(path, identity=identity)
        assert_source_identity(path, identity)
        if keep_alpha and already_transparent(original):
            cutout = original
        else:
            if model not in sessions:
                sessions[model] = session_factory(model)
            cutout = extract(original, sessions[model], edge_mode)
        # Never poison the cache if the source changed during extraction.
        assert_source_identity(path, identity)
        try:
            disk_cache.save(path, model, keep_alpha, edge_mode, cutout, identity=identity)
        except OSError:
            pass  # A cache write failure must not prevent exporting an image.
    assert_source_identity(path, identity)
    memory.pop(key, None)
    memory[key] = cutout
    while len(memory) > memory_limit:
        memory.pop(next(iter(memory)))
    return cutout
