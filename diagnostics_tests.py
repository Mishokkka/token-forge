"""Run all required product test suites without false successful diagnostics."""
import sys
import unittest
from pathlib import Path

REQUIRED_SUITES = ('test_engine.py', 'test_items.py', 'test_cli.py', 'test_cache.py', 'test_settings.py',
                   'test_image_normalization.py', 'test_concurrency.py')


class ExecutionResult(unittest.TextTestResult):
    """Count actual test outcomes, not discovered or setup-skipped cases."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed_modules = set()

    def _record(self, test):
        self.executed_modules.add(test.__class__.__module__.split('.')[-1])

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is None:
            self._record(test)


def run_tests(directory=Path(__file__).resolve().parent):
    directory = Path(directory)
    missing = [name for name in REQUIRED_SUITES if not (directory / name).is_file()]
    if missing:
        print('Python imports passed; required regression suites missing: ' + ', '.join(missing), file=sys.stderr)
        return 2
    loader = unittest.TestLoader()
    suite = loader.discover(str(directory))
    if loader.errors:
        unittest.TextTestRunner(verbosity=2).run(suite)
        return 1
    def cases(group):
        for item in group:
            if isinstance(item, unittest.TestSuite):
                yield from cases(item)
            else:
                yield item
    modules = {test.__class__.__module__.split('.')[-1] for test in cases(suite)}
    absent = [Path(name).stem for name in REQUIRED_SUITES if Path(name).stem not in modules]
    if absent:
        print('Required suites not discovered: ' + ', '.join(absent), file=sys.stderr)
        return 2
    if suite.countTestCases() == 0:
        print('Python imports passed; no regression tests were found. Tests did not run.', file=sys.stderr)
        return 2
    result = unittest.TextTestRunner(verbosity=2, resultclass=ExecutionResult).run(suite)
    if not result.wasSuccessful():
        return 1
    unexecuted = [Path(name).stem for name in REQUIRED_SUITES
                  if Path(name).stem not in result.executed_modules]
    if unexecuted:
        print('Required suites did not execute any non-skipped tests: ' + ', '.join(unexecuted), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(run_tests())
