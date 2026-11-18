"""Shared preference directory without eagerly resolving the home folder."""
import os
from pathlib import Path


def preferences_dir():
    local = os.environ.get('LOCALAPPDATA')
    return (Path(local) if local else Path.home() / '.config') / 'TokenForge'
