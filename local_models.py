"""Only explicitly supported local inference backends may be instantiated."""
from functools import lru_cache


LOCAL_MODELS = ('birefnet-general', 'birefnet-portrait', 'bria-rmbg',
                'birefnet-general-lite', 'isnet-general-use', 'isnet-anime', 'u2net')
DEFAULT_MODEL = LOCAL_MODELS[0]


@lru_cache(maxsize=1)
def _inference_versions():
    # Loaded Python/native packages do not change inside an active process.
    from importlib.metadata import version, PackageNotFoundError
    versions = []
    for package in ('rembg', 'onnxruntime', 'Pillow', 'numpy', 'scipy', 'scikit-image', 'pymatting'):
        try:
            versions.append((package, version(package)))
        except PackageNotFoundError:
            versions.append((package, 'not-installed'))
    return tuple(versions)


def model_candidates(name):
    """Match the pinned rembg resolver, including empty environment roots."""
    import os
    from pathlib import Path
    require_local_model(name)
    xdg = os.environ.get('XDG_DATA_HOME')
    explicit = os.environ.get('U2NET_HOME')
    # Match rembg's getenv semantics, including deliberately empty values.
    legacy = Path(os.environ.get('U2NET_HOME',
                                 os.path.join(os.environ.get('XDG_DATA_HOME', '~'), '.u2net'))).expanduser()
    default_home = os.path.join(xdg, 'rembg') if xdg else '~/.rembg'
    home = Path(explicit or os.environ.get('REMBG_HOME', default_home)).expanduser()
    return home / 'models' / name / (name + '.onnx'), legacy / (name + '.onnx')


def selected_model_file(name):
    """Return the exact candidate that rembg would select, and its stat."""
    for path in model_candidates(name):
        try:
            st = path.stat()
        except OSError:
            # rembg's os.path.exists() treats inaccessible candidates as
            # absent. Preserve its precedence and readable legacy fallback.
            continue
        return path, st
    return None


def model_revision(name):
    """Cheap metadata revision of selected weights; no imports or downloads."""
    selected = selected_model_file(name)
    if selected is None:
        return _inference_versions(), ()
    path, st = selected
    stamp = (str(path.absolute()), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return _inference_versions(), (stamp,)


def weights_status(name):
    """Presence is not proof of model integrity or a loaded ONNX session."""
    import stat
    selected = selected_model_file(name)
    if selected is None:
        return 'missing'
    _, info = selected
    return 'local' if stat.S_ISREG(info.st_mode) and info.st_size > 0 else 'invalid'


def require_local_model(name):
    if not isinstance(name, str) or name not in LOCAL_MODELS:
        raise ValueError(f'Неподдерживаемая локальная модель: {name!r}. Выбери модель в настройках.')
    return name


def call_rembg(function, *args, **kwargs):
    """Foreign library failures must be ordinary, reportable app errors.

    Some rembg versions terminate the process during backend import instead
    of raising ImportError. Do not allow that to escape a worker or CLI.
    """
    try:
        from importlib import import_module
        backend = import_module('rembg')
        operation = getattr(backend, function)
        return operation(*args, **kwargs)
    except (ImportError, SystemExit) as exc:
        raise RuntimeError('Не удалось запустить локальную обработку rembg/onnxruntime. '
                           'Проверь зависимости через check_install.bat и install.bat.') from exc


def new_local_session(name):
    require_local_model(name)
    return call_rembg('new_session', name)
