import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from diagnostics_tests import REQUIRED_SUITES, run_tests
from python_compat import compatibility_error


class DiagnosticsTests(unittest.TestCase):
    def test_only_diagnostics_test_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'test_diagnostics.py').write_text('import unittest\n', encoding='utf-8')
            self.assertEqual(run_tests(root), 2)

    def _fixture(self, root, skip=None, fail=None):
        cases = []
        for name in REQUIRED_SUITES:
            (root / name).touch()
            stem = Path(name).stem
            def test(self, name=stem):
                if name == fail:
                    self.fail('intentional regression')
            case = type('RegressionCase', (unittest.TestCase,),
                        {'__module__': stem, 'test_example': test})
            if stem == skip:
                case = unittest.skip('suite unavailable')(case)
            cases.append(case('test_example'))
        return unittest.TestSuite(cases)

    def test_empty_discovery_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in REQUIRED_SUITES:
                (root / name).touch()
            with patch('unittest.TestLoader.discover', return_value=unittest.TestSuite()) as discover:
                self.assertEqual(run_tests(root), 2)
            discover.assert_called_once_with(str(root))

    def test_discovered_but_skipped_suite_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for skipped, failed, expected in ((None, None, 0), ('test_cache', None, 2),
                                              ('test_cache', 'test_engine', 1)):
                suite = self._fixture(root, skip=skipped, fail=failed)
                output = io.StringIO()
                with patch('unittest.TestLoader.discover', return_value=suite), redirect_stderr(output):
                    self.assertEqual(run_tests(root), expected)
                if expected == 2:
                    self.assertIn('test_cache', output.getvalue())

    def test_windows_3130_rejected_but_3131_accepted(self):
        with patch('python_compat.sys.platform', 'win32'), \
             patch('python_compat.sys.implementation', SimpleNamespace(name='cpython')), \
             patch('python_compat.struct.calcsize', return_value=8), \
             patch('python_compat.sysconfig.get_config_var', return_value=0), \
             patch.dict('sys.modules', {'tkinter': SimpleNamespace(Tcl=lambda: None, TclError=RuntimeError)}):
            with patch('python_compat.sys.version_info', (3, 13, 0, 'final', 0)):
                self.assertIn('3.13.1', compatibility_error())
            with patch('python_compat.sys.version_info', (3, 13, 1, 'final', 0)):
                self.assertIsNone(compatibility_error())
            with patch('python_compat.sys.version_info', (3, 12, 4, 'final', 0)), \
                 patch.dict('sys.modules', {'tkinter': None}):
                self.assertIn('Tcl/Tk', compatibility_error())


if __name__ == '__main__':
    unittest.main()
