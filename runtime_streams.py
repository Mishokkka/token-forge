"""Provide real standard I/O streams for Windows' console-less pythonw.exe."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def initialize_missing_streams(log_path: Path | None = None) -> None:
    """Leave console streams alone; route missing output to a local log.

    Called before importing optional GUI/AI dependencies. The streams remain
    open for the lifetime of the process, including third-party exit handlers.
    """
    if sys.stdout is None or sys.stderr is None:
        if log_path is None:
            log_path = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.config')) / 'TokenForge' / 'context_runtime.log'
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            output = log_path.open('a', encoding='utf-8', errors='backslashreplace', buffering=1)
        except OSError:
            output = open(os.devnull, 'w', encoding='utf-8', errors='backslashreplace', buffering=1)
        if sys.stdout is None:
            sys.stdout = output
        if sys.stderr is None:
            sys.stderr = output
    if sys.stdin is None:
        sys.stdin = open(os.devnull, 'r', encoding='utf-8')
    for name in ('stdin', 'stdout', 'stderr'):
        if getattr(sys, f'__{name}__') is None:
            setattr(sys, f'__{name}__', getattr(sys, name))
