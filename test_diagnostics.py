import tempfile
import unittest
from pathlib import Path

from diagnostics_tests import run_tests


class DiagnosticsTests(unittest.TestCase):
    def test_only_diagnostics_test_is_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'test_diagnostics.py').write_text('import unittest\n', encoding='utf-8')
            self.assertEqual(run_tests(root), 2)

    def test_empty_suite_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(run_tests(Path(temp)), 2)


if __name__ == '__main__':
    unittest.main()
