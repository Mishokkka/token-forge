"""Real OS process-lock and non-overwriting publication contracts.

Children are bounded by timeouts and killed on failure. No large allocations
or model weights are needed, and no user settings directory is accessed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from PIL import Image

from process_lock import process_lock
from token_engine import read_export_owner, recipe_digest, source_identity

ROOT = Path(__file__).resolve().parent
MODEL = 'birefnet-general'

CHILD = """
import json
import sys
import time
from pathlib import Path
from PIL import Image
import token_engine as engine
from cutout_service import load_cutout

mode, root, label = sys.argv[1:]
root = Path(root)
source = root / 'Hero.png'
cache = engine.DiskCutoutCache(root / 'cutouts', max_entries=1)
identity = engine.source_identity(source)
model = 'birefnet-general'
edge = 'Очистка краёв'
def forbidden(*args, **kwargs):
    raise AssertionError('unexpected inference')

if mode == 'alpha':
    result = load_cutout(source, model, True, edge,
                         key=(*identity, model, True, edge),
                         disk_cache=cache, memory={}, sessions={},
                         decode=engine.read_image, extract=forbidden, session_factory=forbidden)
    assert result.image.getpixel((0, 0))[3] == 80
    assert not list(cache.directory.glob('*.png'))
    print(json.dumps({'alpha': 80}))
elif mode == 'cache':
    cache.save(source, model, True, edge, Image.new('RGBA', (8, 8), label),
               identity=identity, revision=('offline-test', label))
    print(json.dumps({'cache': True}))
elif mode == 'publication':
    original = engine._publish_new
    def contend(temporary, destination):
        (root / (label + '.ready')).touch()
        other = 'red' if label == 'blue' else 'blue'
        deadline = time.monotonic() + 15
        while not (root / (other + '.ready')).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('peer never reached publication')
            time.sleep(.02)
        return original(temporary, destination)
    engine._publish_new = contend
    result = engine.save_generated(Image.new('RGBA', (16, 16), label),
                                   root / 'Herot.webp', source, 'token',
                                   skip_exists=True, input_identity=identity,
                                   recipe=engine.recipe_digest('token', version='same'))
    print(json.dumps({'created': result.created, 'warnings': result.warnings, 'color': label}))
else:
    raise AssertionError(mode)
"""


class ProcessContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='TokenForge processes ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'Hero.png'
        Image.new('RGBA', (8, 8), (20, 30, 40, 80)).save(self.source)
        # Isolate all model revision probes from real local weights.
        self.env = dict(os.environ, PYTHONUTF8='1', U2NET_HOME=str(self.root / 'models'))

    def command(self, mode, color='red'):
        return [sys.executable, '-c', CHILD, mode, str(self.root), color]

    def run_child(self, mode):
        result = subprocess.run(self.command(mode), cwd=ROOT, env=self.env,
                                capture_output=True, encoding='utf-8', timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def run_pair(self, mode):
        workers = []
        try:
            for color in ('red', 'blue'):
                workers.append(subprocess.Popen(self.command(mode, color), cwd=ROOT, env=self.env,
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                encoding='utf-8'))
            reports = []
            for child in workers:
                out, err = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, out + err)
                reports.append(json.loads(out))
            return reports
        finally:
            for child in workers:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_transparent_source_does_not_wait_for_other_process_inference(self):
        with process_lock(self.root / 'inference', timeout=0):
            self.assertEqual(self.run_child('alpha')['alpha'], 80)

    def test_cache_capacity_is_shared_by_os_processes(self):
        with process_lock(self.root / 'cutouts' / '.maintenance', timeout=0):
            self.run_child('cache')
            self.assertFalse(list((self.root / 'cutouts').glob('*.png')))
        self.run_pair('cache')
        entries = list((self.root / 'cutouts').glob('*.png'))
        self.assertEqual(len(entries), 1)
        with Image.open(entries[0]) as image:
            image.load()
            self.assertEqual(image.size, (8, 8))

    def test_two_real_exporters_do_not_clobber_the_atomic_winner(self):
        reports = self.run_pair('publication')
        winners = [report for report in reports if report['created']]
        self.assertEqual(len(winners), 1)
        self.assertFalse(any(report['warnings'] for report in reports))
        destination = self.root / 'Herot.webp'
        owner = read_export_owner(destination)
        self.assertEqual(owner[3], source_identity(self.source)[-1])
        self.assertEqual(owner[4], recipe_digest('token', version='same'))
        with Image.open(destination) as image:
            self.assertEqual(image.convert('RGB').getpixel((0, 0)),
                             (255, 0, 0) if winners[0]['color'] == 'red' else (0, 0, 255))


if __name__ == '__main__':
    unittest.main()
