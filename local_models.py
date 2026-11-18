"""Only explicitly supported local inference backends may be instantiated."""
LOCAL_MODELS = ('birefnet-general', 'birefnet-portrait', 'bria-rmbg',
                'birefnet-general-lite', 'isnet-general-use', 'isnet-anime', 'u2net')
DEFAULT_MODEL = LOCAL_MODELS[0]


def require_local_model(name):
    if not isinstance(name, str) or name not in LOCAL_MODELS:
        raise ValueError(f'Неподдерживаемая локальная модель: {name!r}. Выбери модель в настройках.')
    return name


def new_local_session(name):
    require_local_model(name)
    from rembg import new_session
    return new_session(name)
