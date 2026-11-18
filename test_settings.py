"""Preferences regression tests, including competing OS-level writers."""
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from settings_store import read_settings_file, save_settings_delta, settings_lock


def write_from_process(path, field, value, overrides, start):
    start.wait()
    save_settings_delta(path, {}, {field: value, 'overrides': overrides})


def check_lock(path, results):
    try:
        with settings_lock(path, timeout=0):
            results.put('acquired')
    except TimeoutError:
        results.put('blocked')


class SettingsTests(unittest.TestCase):
    def test_explicit_reset_of_remote_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            empty = {'overrides': {}}
            save_settings_delta(path, empty, {'overrides': {'a': {'x': 12}, 'b': {'y': 4}}})
            self.assertTrue(save_settings_delta(path, empty, empty, resets={('overrides', 'a')}))
            self.assertEqual(read_settings_file(path), {'overrides': {'b': {'y': 4}}})

    def test_reset_unchanged_override_and_subsequent_edit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            initial = {'overrides': {'a': {'x': 12, 'y': 3}}}
            for current, expected in (
                (initial, {}),
                ({'overrides': {'a': {'x': 24, 'y': 3}}}, {'a': {'x': 24}}),
            ):
                path.write_text(json.dumps(initial), encoding='utf-8')
                self.assertTrue(save_settings_delta(path, initial, current, resets={('overrides', 'a')}))
                self.assertEqual(read_settings_file(path), {'overrides': expected})

    def test_unpaired_surrogate_round_trips_as_escape(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            path.write_text(r'{"extra":"\ud800"}', encoding='utf-8')
            baseline = read_settings_file(path, strict=True)
            self.assertTrue(save_settings_delta(path, baseline, {**baseline, 'size': 512}))
            self.assertEqual(read_settings_file(path), {'extra': '\ud800', 'size': 512})

    def test_lock_excludes_other_process_until_release(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            ctx = multiprocessing.get_context('spawn')
            results = ctx.Queue()
            def run():
                proc = ctx.Process(target=check_lock, args=(path, results))
                proc.start()
                try:
                    result = results.get(timeout=10)
                    proc.join(10)
                    self.assertEqual(proc.exitcode, 0)
                    return result
                finally:
                    if proc.is_alive():
                        proc.terminate()
                        proc.join()
            with settings_lock(path):
                self.assertEqual(run(), 'blocked')
            self.assertEqual(run(), 'acquired')

    def test_invalid_top_level_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            for value in (None, [], 8):
                path.write_text(json.dumps(value), encoding='utf-8')
                self.assertEqual(read_settings_file(path), {})
                with self.assertRaises(ValueError):
                    read_settings_file(path, strict=True)
                with self.assertRaises(ValueError):
                    save_settings_delta(path, {}, {'size': 256})
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), value)

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
            started = []
            try:
                for proc in (a, b):
                    proc.start()
                    started.append(proc)
                start.set()
                for proc in started:
                    proc.join(10)
                    self.assertEqual(proc.exitcode, 0)
            finally:
                for proc in started:
                    if proc.is_alive():
                        proc.terminate()
                    proc.join()
            self.assertEqual(read_settings_file(path), {'size': 512, 'ext': 'png',
                'overrides': {'same': {'x': 12, 'y': 23}}})


if __name__ == '__main__':
    unittest.main()
