"""Only explicitly supported local inference backends may be instantiated."""
LOCAL_MODELS = ('birefnet-general', 'birefnet-portrait', 'bria-rmbg',
                'birefnet-general-lite', 'isnet-general-use', 'isnet-anime', 'u2net')
DEFAULT_MODEL = LOCAL_MODELS[0]


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
