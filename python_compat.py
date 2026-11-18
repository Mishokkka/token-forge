"""Supported interpreter contract shared by setup probes."""
import struct
import sys
import sysconfig


def supported():
    return (sys.implementation.name == 'cpython'
            and (3, 11) <= sys.version_info[:2] <= (3, 13)
            and struct.calcsize('P') == 8
            and not sysconfig.get_config_var('Py_GIL_DISABLED'))


if __name__ == '__main__':
    raise SystemExit(0 if supported() else 1)
