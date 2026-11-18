"""Only explicitly supported local inference backends may be instantiated."""
LOCAL_MODELS = ('birefnet-general', 'birefnet-portrait', 'bria-rmbg',
                'birefnet-general-lite', 'isnet-general-use', 'isnet-anime', 'u2net')
DEFAULT_MODEL = LOCAL_MODELS[0]


def model_revision(name):
    """Cheap local model revision; never import rembg or download weights.

    Cover the legacy flat model directory and newer per-model layout.
    Package versions and file metadata invalidate caches on ordinary upgrades
    or replacement. This is not a cryptographic verification of model weights.
    """
    import os
    from pathlib import Path
    from importlib.metadata import version, PackageNotFoundError
    require_local_model(name)
    versions = []
    for package in ('rembg', 'onnxruntime'):
        try:
            versions.append(version(package))
        except PackageNotFoundError:
            versions.append('not-installed')
    xdg = os.environ.get('XDG_DATA_HOME')
    explicit = os.environ.get('U2NET_HOME')
    legacy = Path(explicit or (Path(xdg or '~') / '.u2net')).expanduser()
    home = Path(explicit or os.environ.get('REMBG_HOME') or
                (Path(xdg) / 'rembg' if xdg else '~/.rembg')).expanduser()
    candidates = (home / 'models' / name / (name + '.onnx'),
                  legacy / (name + '.onnx'), home / (name + '.onnx'))
    stamps = []
    for path in dict.fromkeys(candidates):
        try:
            st = path.stat()
            stamps.append((str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns))
        except OSError:
            continue
    return tuple(versions), tuple(stamps)


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
