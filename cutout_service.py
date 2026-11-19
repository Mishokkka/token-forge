"""Shared extraction/cache policy for GUI and Explorer/CLI entry points."""
from dataclasses import dataclass
from contextlib import ExitStack
from PIL import Image
from token_engine import already_transparent, assert_source_identity, validate_art_dimensions
from local_models import require_local_model, model_revision
from process_lock import process_lock
from operation_control import check_cancel
from resource_policy import MEMORY_CACHE_BYTES as MEMORY_LIMIT_BYTES, validate_extraction_dimensions


@dataclass(frozen=True)
class CutoutResult:
    image: Image.Image | None
    revision: tuple


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
                verify=None, cancel_event=None, prepare_model=None, on_wait=None):
    check_cancel(cancel_event)
    require_local_model(model)
    lock_options = {} if cancel_event is None else {'cancel_event': cancel_event}
    identity = key[:4]
    check = verify if verify is not None else lambda: assert_source_identity(path, identity)
    revision = model_revision(model)
    memory_key = (*key, revision)

    def cached():
        image = memory.get(memory_key)
        if image is not None:
            return image
        return disk_cache.load(path, model, keep_alpha, edge_mode, identity=identity, revision=revision)

    def verify_revision():
        if model_revision(model) != revision:
            sessions.clear()
            raise OSError('Файлы модели изменились во время удаления фона. Повтори обработку.')

    def publish(image):
        validate_art_dimensions(image)
        verify_revision()
        try:
            disk_cache.save(path, model, keep_alpha, edge_mode, image,
                            identity=identity, revision=revision)
        except OSError:
            # Caching is optional, source verification is not.
            check()
        verify_revision()

    cutout = cached()
    check_cancel(cancel_event)
    if cutout is None:
        if cached_only:
            check()
            verify_revision()
            return CutoutResult(None, revision)
        # Try an immediate reservation for possible alpha bypasses. If
        # uncontended, the second cache lookup still precedes source decoding.
        lock_path = disk_cache.directory.parent / 'inference'
        original = None
        with ExitStack() as reservation:
            try:
                if on_wait is not None:
                    on_wait(model)
                reservation.enter_context(process_lock(lock_path, timeout=0 if keep_alpha else 120, **lock_options))
            except OSError as lock_error:
                if not keep_alpha:
                    raise
                revision = model_revision(model)
                memory_key = (*key, revision)
                original = decode(path, identity=identity)
                validate_art_dimensions(original)
                if already_transparent(original):
                    # No inference and no contested disk publication.
                    check()
                    verify_revision()
                    remember(memory, memory_key, original, count_limit=memory_limit)
                    return CutoutResult(original, revision)
                if not isinstance(lock_error, TimeoutError):
                    raise
                # No full pixel buffer may survive a blocking wait. A cache
                # hit after the reservation needs no further decoding.
                original = None
                reservation.enter_context(process_lock(lock_path, timeout=120, **lock_options))
            revision = model_revision(model)
            memory_key = (*key, revision)
            cutout = cached()
            if cutout is None:
                if original is None:
                    original = decode(path, identity=identity)
                validate_art_dimensions(original)
                if keep_alpha and already_transparent(original):
                    cutout = original
                else:
                    # Reject expensive work before allocating a native session.
                    # The public remove_background entry point checks this too.
                    validate_extraction_dimensions(original, edge_mode)
                    existing = sessions.get(model)
                    if existing is not None and existing[0] == revision:
                        session = existing[1]
                    else:
                        # Release previous native sessions before allocating another.
                        sessions.clear()
                        del existing
                        if prepare_model is not None:
                            # Download/verify first, so the initial native session
                            # need not be instantiated twice on a first download.
                            prepare_model(model)
                            revision = model_revision(model)
                        check_cancel(cancel_event)
                        for attempt in range(2):
                            session = session_factory(model)
                            observed = model_revision(model)
                            if observed == revision:
                                break
                            # A first download may legitimately change the revision.
                            # Never relabel a session that loaded an unknown version.
                            del session
                            revision = observed
                        else:
                            raise OSError('Файлы модели нестабильны. Повтори обработку.')
                        memory_key = (*key, revision)
                        sessions[model] = (revision, session)
                    check_cancel(cancel_event)
                    cutout = extract(original, session, edge_mode)
                check_cancel(cancel_event)
                publish(cutout)

    check_cancel(cancel_event)
    # Also verify hits (including the second lookup under the inference lock).
    check()
    verify_revision()
    remember(memory, memory_key, cutout, count_limit=memory_limit)
    return CutoutResult(cutout, revision)
