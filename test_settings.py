"""Preferences regression tests, including competing OS-level writers."""
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from settings_store import read_settings_file, save_settings_delta


def write_from_process(path, field, value, overrides, start):
    start.wait()
    save_settings_delta(path, {}, {field: value, 'overrides': overrides})


class SettingsTests(unittest.TestCase):
    def test_invalid_top_level_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            for value in (None, [], 8):
                path.write_text(json.dumps(value), encoding='utf-8')
                self.assertEqual(read_settings_file(path), {})

    def test_unchanged_close_and_independent_edits(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            initial = {'size': 256, 'overrides': {'a': {'x': 0, 'y': 0}}}
            path.write_text(json.dumps(initial), encoding='utf-8')
            first = {'size': 512, 'overrides': {'a': {'x': 10, 'y': 0}, 'b': {'zoom': 90}}}
            second = {'size': 256, 'overrides': {'a': {'x': 0, 'y': 20}, 'c': {'zoom': 60}}}
            self.assertTrue(save_settings_delta(path, initial, first))
            self.assertTrue(save_settings_delta(path, initial, second))
            self.assertFalse(save_settings_delta(path, initial, initial))
            final = read_settings_file(path)
            self.assertEqual(final, {'size': 512, 'overrides': {
                'a': {'x': 10, 'y': 20}, 'b': {'zoom': 90}, 'c': {'zoom': 60}}})
            reset = {'size': 256, 'overrides': {'c': {'zoom': 60}}}
            self.assertTrue(save_settings_delta(path, second, reset))
            self.assertNotIn('a', read_settings_file(path)['overrides'])
            self.assertIn('b', read_settings_file(path)['overrides'])

    def test_parallel_processes_merge_new_override_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            ctx = multiprocessing.get_context('spawn')
            start = ctx.Event()
            a = ctx.Process(target=write_from_process, args=(path, 'size', 512, {'same': {'x': 12}}, start))
            b = ctx.Process(target=write_from_process, args=(path, 'ext', 'png', {'same': {'y': 23}}, start))
            for proc in (a, b):
                proc.start()
            start.set()
            try:
                for proc in (a, b):
                    proc.join(10)
                    self.assertEqual(proc.exitcode, 0)
            finally:
                for proc in (a, b):
                    if proc.is_alive():
                        proc.terminate()
                        proc.join()
            self.assertEqual(read_settings_file(path), {'size': 512, 'ext': 'png',
                'overrides': {'same': {'x': 12, 'y': 23}}})


if __name__ == '__main__':
    unittest.main()
