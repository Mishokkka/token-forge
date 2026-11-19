"""Offline real-spawn process and streamed download contracts."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from inference_worker import InferenceClient
from local_models import model_candidates, weights_status
from model_download import download_model, MODEL_ASSETS
from operation_control import OperationCancelled
from process_lock import process_lock


MODEL = 'birefnet-portrait'


def synthetic_worker(pipe):
    """Real child process; no AI/network. Tests transport and cancellation."""
    try:
        while True:
            command, args = pipe.recv()
            name = args[0]
            if command == 'download':
                Path(args[1]).write_bytes(b'partial')
                pipe.send(('stage', (name, 'download', 7, 100, 7.0)))
                time.sleep(60)
            elif command == 'initialize':
                pipe.send(('stage', (name, 'initialize', 0, 0, 0.0)))
                pipe.send(('stage', (name, 'ready', 0, 0, 0.0)))
                pipe.send(('ok', None))
            elif command == 'extract':
                pipe.send(('stage', (name, 'extract', 0, 0, 0.0)))
                if args[3] == 'hang':
                    time.sleep(60)
                if args[3] == 'crash':
                    os._exit(17)
                with Image.open(args[1]) as source:
                    result = source.convert('RGBA')
                    result.putalpha(123)
                    result.save(args[2])
                pipe.send(('ok', None))
    except (EOFError, OSError):
        pass
    finally:
        pipe.close()


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {} if length is None else {'Content-Length': str(length)}


class InferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='TokenForge model ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = patch.dict(os.environ, {'U2NET_HOME': str(self.root)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.destination = model_candidates(MODEL)[0]
        self.destination.parent.mkdir(parents=True)
        self.cancel = threading.Event()
        self.events = []
        self.client = InferenceClient(self.cancel, lambda *args: self.events.append(args),
                                      target=synthetic_worker)
        self.addCleanup(self.client.close)

    def test_manifest_matches_supported_models(self):
        from local_models import LOCAL_MODELS
        self.assertEqual(set(MODEL_ASSETS), set(LOCAL_MODELS))

    def test_verified_download_and_legacy_reuse(self):
        content = b'weights' * 100
        staging = self.destination.parent / 'owned.part'
        digest = hashlib.sha256(content).hexdigest()
        for length in (len(content), None):
            with self.subTest(length=length):
                with patch.dict(MODEL_ASSETS, {MODEL: ('test.onnx', 'sha256:' + digest)}):
                    download_model(MODEL, staging, lambda *args: self.events.append(args),
                                   opener=lambda *a, **k: Response(content, length))
                self.assertEqual(self.destination.read_bytes(), content)
                self.assertEqual(weights_status(MODEL), 'local')
                self.assertEqual(self.events[-1][:3], ('verify', len(content), len(content)))
                self.destination.unlink()
                staging.unlink(missing_ok=True)
        legacy = model_candidates(MODEL)[1]
        legacy.write_bytes(b'legacy')
        download_model(MODEL, staging, lambda *args: None,
                       opener=lambda *a, **k: self.fail('legacy weights must not download'))
        self.assertFalse(self.destination.exists())

    def test_truncated_and_wrong_hash_never_publish(self):
        staging = self.destination.parent / 'owned.part'
        for length in (9, 3, None):
            with self.subTest(length=length):
                with self.assertRaises(OSError):
                    download_model(MODEL, staging, lambda *args: None,
                                   opener=lambda *a, **k: Response(b'bad', length))
                self.assertFalse(self.destination.exists())
        # Existing arbitrary user-supplied files are never replaced.
        self.destination.write_bytes(b'keep')
        download_model(MODEL, staging, lambda *args: None,
                       opener=lambda *a, **k: self.fail('unexpected network'))
        self.assertEqual(self.destination.read_bytes(), b'keep')

    def test_cancel_download_reaps_child_and_staging(self):
        def emit(*data):
            self.events.append(data)
            if data[1] == 'download':
                self.cancel.set()
        self.client.emit = emit
        start = time.monotonic()
        with self.assertRaises(OperationCancelled):
            self.client.prepare(MODEL)
        self.assertLess(time.monotonic() - start, 15)
        self.assertIsNone(self.client.process)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.iterdir()), [])

    def test_native_cancel_and_crash_allow_retry_and_preserve_weights(self):
        self.destination.write_bytes(b'preseeded')
        self.assertEqual(self.client.session(MODEL), MODEL)
        image = Image.new('RGBA', (12, 9), 'red')
        first_pid = self.client.process.pid
        for _ in range(2):
            result = self.client.extract(image, MODEL, 'normal')
            self.assertEqual(result.getchannel('A').getextrema(), (123, 123))
            self.assertEqual(self.client.process.pid, first_pid)
        def emit(*data):
            if data[1] == 'extract':
                self.cancel.set()
        self.client.emit = emit
        with self.assertRaises(OperationCancelled):
            self.client.extract(image, MODEL, 'hang')
        self.assertIsNone(self.client.process)
        self.assertEqual(self.destination.read_bytes(), b'preseeded')
        self.cancel.clear()
        self.client.emit = lambda *data: self.events.append(data)
        with self.assertRaises(RuntimeError):
            self.client.extract(image, MODEL, 'crash')
        self.assertIsNone(self.client.process)
        result = self.client.extract(image, MODEL, 'normal')
        self.assertEqual(result.getchannel('A').getextrema(), (123, 123))

    def test_cancel_lock_wait(self):
        self.cancel.set()
        with self.assertRaises(OperationCancelled):
            with process_lock(self.root / 'lock', timeout=120, cancel_event=self.cancel):
                self.fail('cancelled reservation acquired')

    def test_prepare_download_revision_precedes_session_once(self):
        from cutout_service import load_cutout
        from token_engine import DiskCutoutCache, source_identity
        from unittest.mock import Mock
        source = self.root / 'art.png'
        Image.new('RGB', (12, 9), 'red').save(source)
        identity = source_identity(source)
        prepare = Mock(side_effect=lambda model: self.destination.write_bytes(b'weights'))
        factory = Mock(return_value='session')
        output = Image.new('RGBA', (12, 9), (1, 2, 3, 100))
        extractor = Mock(return_value=output)
        result = load_cutout(source, MODEL, False, 'Обычный',
                             key=(*identity, MODEL, False, 'Обычный'),
                             disk_cache=DiskCutoutCache(self.root / 'cutouts'),
                             memory={}, sessions={},
                             decode=lambda path, **kwargs: Image.open(path).convert('RGBA'),
                             extract=extractor, session_factory=factory,
                             prepare_model=prepare, cancel_event=self.cancel)
        self.assertIs(result.image, output)
        prepare.assert_called_once_with(MODEL)
        factory.assert_called_once_with(MODEL)

    def test_cancelled_export_reports_cancel_not_error(self):
        from item_engine import ItemAdjust, export_items, item_output_path
        source = self.root / 'art.png'
        Image.new('RGB', (12, 9), 'red').save(source)
        def cancel(path):
            self.cancel.set()
            raise OperationCancelled()
        report = export_items([source], cancel, {source: ItemAdjust()}, convert_original=False,
                              cancel_event=self.cancel)
        self.assertTrue(report.cancelled)
        self.assertFalse(report.errors)
        self.assertFalse(item_output_path(source).exists())


if __name__ == '__main__':
    unittest.main()
