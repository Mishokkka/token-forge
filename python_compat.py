"""Shared interpreter and Tcl prerequisites for setup and diagnostics."""
import struct
import sys
import sysconfig


def compatibility_error():
    if (sys.implementation.name != 'cpython'
            or not (3, 11) <= sys.version_info[:2] <= (3, 13)
            or struct.calcsize('P') != 8
            or sysconfig.get_config_var('Py_GIL_DISABLED')):
        return 'Use regular 64-bit CPython 3.11-3.13, not a free-threaded build.'
    if sys.platform == 'win32' and sys.version_info[:3] == (3, 13, 0):
        return 'Windows Python 3.13.0 has a Tk virtual-environment bug. Upgrade to 3.13.1 or later before recreating .venv.'
    try:
        import tkinter
    except (ImportError, OSError) as exc:
        return f'Enable Tcl/Tk in the Python installation: {exc}'
    try:
        # Initialize Tcl without requiring a display or opening a window.
        tkinter.Tcl()
    except (tkinter.TclError, RuntimeError, OSError) as exc:
        return f'Tcl/Tk is not usable: {exc}'
    return None


def supported():
    return compatibility_error() is None


if __name__ == '__main__':
    error = compatibility_error()
    if error:
        print(error, file=sys.stderr)
    raise SystemExit(1 if error else 0)
