"""Run the available regression suite without claiming success for zero tests."""
import sys
import unittest
from pathlib import Path


def run_tests(directory=Path(__file__).resolve().parent):
    suite = unittest.defaultTestLoader.discover(str(directory))
    if suite.countTestCases() == 0:
        print('Python imports passed; no regression tests were found. Tests did not run.', file=sys.stderr)
        return 2
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(run_tests())
