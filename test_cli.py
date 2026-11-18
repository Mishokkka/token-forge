"""Offline integration checks for v1.6. Run with: python -m unittest -v test_cli.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

import context_menu
import token_cli
import tokenforge


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='TokenForge test кириллица ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.art_folder = self.root / 'My Portraits' / 'Персонажи'
        self.art_folder.mkdir(parents=True)
        self.base = self.root / 'Program Files' / 'TokenForge'
        self.base.mkdir(parents=True)
        self.ring = Path(__file__).parent / 'RingT.png'
        self.pref = self.root / 'AppData Local' / 'TokenForge'
        self.pref.mkdir(parents=True)
        self.patchers = [
            patch.object(token_cli, 'PREFS_DIR', self.pref),
            patch.object(token_cli, 'PREFS_FILE', self.pref / 'settings.json'),
            patch.object(token_cli, 'LOG_FILE', self.pref / 'context_actions.log'),
            patch.object(token_cli, 'BASE', self.base),
            patch.dict(sys.modules, {'rembg': SimpleNamespace(
                new_session=lambda *a, **k: self.fail('AI session invoked in offline test'))}),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.photo = self.art_folder / 'Великий дракон 01.png'
        image = Image.new('RGBA', (320, 520), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((115, 40, 245, 180), fill=(50, 100, 140, 255))
        draw.rectangle((72, 170, 280, 510), fill=(80, 40, 120, 255))
        image.save(self.photo)
        self.settings = {
            'ring': str(self.ring), 'model': 'birefnet-portrait',
            'edge_mode': 'Очистка краёв', 'keep_alpha': True,
            'convert_original': True, 'skip_exists': True,
            'size': 256, 'ext': 'webp',
            'overrides': {str(self.photo.resolve()): {'zoom': 130, 'x': 12, 'y': -4, 'gap': 8}},
        }
        self.save_settings()

    def save_settings(self):
        (self.pref / 'settings.json').write_text(json.dumps(self.settings, ensure_ascii=False), encoding='utf-8')

    def test_legacy_boolean_string_is_not_truthy_in_auto(self):
        self.settings['convert_original'] = 'false'
        self.settings['skip_exists'] = '0'
        self.save_settings()
        self.assertEqual(token_cli.automatic_token(self.photo)[0], 'created')
        self.assertFalse(self.photo.with_suffix('.webp').exists())
        self.assertEqual(token_cli.automatic_token(self.photo)[0], 'created')

    def test_context_auto_creates_art_and_token(self):
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            state, token = token_cli.automatic_token(self.photo)
        self.assertEqual(state, 'created')
        self.assertEqual(token.name, 'Великий дракон 01t.webp')
        self.assertTrue(token.is_file())
        self.assertTrue(self.photo.with_suffix('.webp').exists())
        with Image.open(token) as image:
            self.assertEqual(image.size, (256, 256))
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.getpixel((0,0))[3], 0)
        self.assertTrue(list((self.pref/'cutouts').glob('*.png')))
        state2, token2 = token_cli.automatic_token(self.photo)
        self.assertEqual(state2, 'skipped')
        self.assertEqual(token2, token)

    def test_backend_system_exit_is_logged_and_returns_failure(self):
        from local_models import new_local_session
        real_import = __import__('importlib').import_module
        def backend(name, *args, **kwargs):
            if name == 'rembg':
                raise SystemExit(1)
            return real_import(name, *args, **kwargs)
        with patch('importlib.import_module', side_effect=backend):
            with self.assertRaisesRegex(RuntimeError, 'rembg/onnxruntime') as caught:
                new_local_session('birefnet-general')
            self.assertIsInstance(caught.exception.__cause__, SystemExit)
            self.settings['keep_alpha'] = False
            self.settings['convert_original'] = False
            self.save_settings()
            with patch.object(token_cli, 'notify_user') as notify:
                self.assertEqual(token_cli.run_auto(self.photo), 1)
                notify.assert_called_once()
            self.assertIn('rembg/onnxruntime', token_cli.LOG_FILE.read_text(encoding='utf-8'))
        self.assertFalse((self.photo.parent / (self.photo.stem + 't.webp')).exists())

    def test_gui_open_source_survives_invalid_settings(self):
        token_cli.PREFS_FILE.write_text('{broken JSON', encoding='utf-8')
        self.assertEqual(token_cli.resolve_context_source(self.photo), self.photo)
        with self.assertRaises(ValueError):
            token_cli.automatic_token(self.photo)

    def test_configured_frame_cannot_be_used_as_art(self):
        self.settings['ring'] = str(self.photo)
        self.save_settings()
        for action in (token_cli.automatic_token, token_cli.automatic_item):
            with self.assertRaisesRegex(ValueError, 'рамка'):
                action(self.photo)

    def test_generated_result_keeps_logical_source_alias_and_output_folder(self):
        # Directory junctions work on Windows without developer-mode symlink
        # privileges and give a real alias to the source's parent directory.
        import os
        import subprocess
        target = self.root / 'Real'
        target.mkdir()
        original = target / 'Original.png'
        original.write_bytes(self.photo.read_bytes())
        alias = self.art_folder / 'Alias'
        if sys.platform == 'win32':
            subprocess.run([os.environ['COMSPEC'], '/d', '/c', 'mklink', '/J',
                            str(alias), str(target)], check=True, capture_output=True)
            self.addCleanup(lambda: alias.rmdir() if alias.exists() else None)
        else:
            alias.symlink_to(target, target_is_directory=True)
        source = alias / 'Original.png'
        self.assertEqual(token_cli.resolve_context_source(source), source)
        _, output = token_cli.automatic_token(source)
        self.assertEqual(output.parent, alias)
        self.assertEqual(token_cli.resolve_context_source(output), source)
        self.assertEqual(token_cli.automatic_token(output), ('skipped', output))

    def test_file_symlink_source_is_not_redirected_to_target(self):
        # File symlinks may not be permitted on a Windows runner. Exercise
        # the logical-name contract even there with an actual alias and a
        # simulated resolve() result, without requiring elevated rights.
        alias = self.art_folder / 'Alias.png'
        alias.write_bytes(self.photo.read_bytes())
        real_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.photo if path == alias else real_resolve(path, *args, **kwargs)
        self.settings['convert_original'] = False
        self.save_settings()
        with patch.object(Path, 'resolve', resolve):
            self.assertEqual(token_cli.resolve_context_source(alias), alias)
            status, output = token_cli.automatic_token(alias)
            self.assertEqual((status, output.name), ('created', 'Aliast.webp'))
            self.assertEqual(token_cli.resolve_context_source(output), alias)
            self.assertEqual(token_cli.automatic_token(output)[0], 'skipped')

    def test_existing_copy_and_token_are_resolved_to_original(self):
        token_cli.automatic_token(self.photo)
        webp = self.photo.with_suffix('.webp')
        token = self.photo.with_name(f'{self.photo.stem}t.webp')
        self.assertEqual(token_cli.resolve_context_source(webp), self.photo)
        self.assertEqual(token_cli.resolve_context_source(token), self.photo)
        self.assertEqual(token_cli.automatic_token(webp)[0], 'skipped')
        self.assertFalse((self.art_folder / (self.photo.stem + 'tt.webp')).exists())

    def test_item_cli_creates_500px_image_and_reuses_cache(self):
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            status, output = token_cli.automatic_item(self.photo)
        self.assertEqual(status, 'created')
        self.assertEqual(output.name, self.photo.stem + '_item.webp')
        with Image.open(output) as artwork:
            self.assertEqual(artwork.size, (500,500))
            self.assertEqual(artwork.mode, 'RGBA')
            self.assertEqual(artwork.getpixel((0,0))[3], 0)
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            self.assertEqual(token_cli.automatic_item(output)[0], 'skipped')
        self.assertEqual(token_cli.resolve_context_source(output), self.photo)
        self.settings['skip_exists'] = False
        self.save_settings()
        with patch.object(token_cli, 'read_image', side_effect=AssertionError('Cache miss')):
            self.assertEqual(token_cli.automatic_item(self.photo)[0], 'created')
        self.assertFalse((self.art_folder / (self.photo.stem + '_item_item.webp')).exists())

    def test_gui_open_item_selects_user_target(self):
        instance = SimpleNamespace(run=lambda: None)
        with patch.object(tokenforge, 'TokenForge', return_value=instance) as gui:
            self.assertEqual(tokenforge.main(['--open-item',str(self.photo)]),0)
            self.assertEqual(gui.call_args.kwargs['startup_file'],self.photo)
            self.assertEqual(gui.call_args.kwargs['startup_mode'],'item')

    def test_auto_item_routes_without_starting_gui(self):
        with patch('token_cli.run_auto',return_value=0) as auto,patch.object(tokenforge,'TokenForge') as gui:
            self.assertEqual(tokenforge.main(['--auto-item',str(self.photo)]),0)
            auto.assert_called_once_with(str(self.photo),item=True)
            gui.assert_not_called()

    def test_auto_overwrites_if_skip_disabled(self):
        status, token = token_cli.automatic_token(self.photo)
        self.assertEqual(status, 'created')
        self.settings['skip_exists'] = False
        self.settings['size'] = 512
        self.save_settings()
        with patch.object(token_cli, 'remove_background', side_effect=AssertionError('AI invoked')):
            state, dest = token_cli.automatic_token(self.photo)
        self.assertEqual(state, 'created')
        self.assertEqual(dest, token)
        with Image.open(dest) as image:
            self.assertEqual(image.size, (512, 512))

    def test_jpeg_ending_in_t_is_not_redirected(self):
        ca = self.art_folder / 'Ca.png'
        cat = self.art_folder / 'Cat.jpg'
        Image.new('RGB', (10, 10)).save(ca)
        Image.new('RGB', (10, 10)).save(cat)
        self.assertIn(cat, __import__('token_engine').list_source_images(self.art_folder))
        self.assertEqual(token_cli.resolve_context_source(cat), cat)
        copy = cat.with_suffix('.webp')
        Image.new('RGB', (10, 10)).save(copy)
        self.assertEqual(token_cli.resolve_context_source(copy), copy)

    def test_token_does_not_claim_another_sources_webp_copy(self):
        ca = self.art_folder / 'Ca.png'
        cat = self.art_folder / 'Cat.jpg'
        Image.open(self.photo).save(ca)
        Image.new('RGB', (10, 10)).save(cat)
        with self.assertRaisesRegex(RuntimeError, 'конфликтует'):
            token_cli.automatic_token(ca)
        self.assertFalse(cat.with_suffix('.webp').exists())

    def test_independent_names_and_orphaned_marked_outputs(self):
        from token_engine import list_source_images
        ca = self.art_folder / 'Ca.png'
        cat = self.art_folder / 'Cat.png'
        item = self.art_folder / 'Ca_item.webp'
        for p in (ca, cat, item):
            Image.new('RGBA', (10, 10), 'red').save(p)
        for p in (ca, cat, item):
            self.assertIn(p, list_source_images(self.art_folder))
            self.assertEqual(token_cli.resolve_context_source(p), p)
        _, token = token_cli.automatic_token(self.photo)
        self.photo.unlink()
        self.assertNotIn(token, list_source_images(self.art_folder))
        with self.assertRaises(ValueError):
            token_cli.resolve_context_source(token)

    def test_missing_frame_does_not_block_skip_and_invalid_overrides(self):
        token_cli.automatic_token(self.photo)
        self.settings['ring'] = str(self.base / 'missing.png')
        self.settings['overrides'] = None
        self.settings['item_overrides'] = None
        self.save_settings()
        self.assertEqual(token_cli.automatic_token(self.photo)[0], 'skipped')
        self.assertEqual(token_cli.automatic_item(self.photo)[0], 'created')

    def test_shared_adjustment_schema_keeps_valid_fields_and_rejects_unsafe_format(self):
        from settings_store import adjustment_for, output_format
        from item_engine import ITEM_MODES
        key = str(self.photo.resolve())
        self.settings['overrides'][key]['x'] = 'bad'
        self.settings['item_overrides'] = {key: {'layout': 'bad', 'rotation': 22, 'zoom': 'bad',
                                                 'flip_horizontal': 'false'}}
        self.settings['item_layout'] = ITEM_MODES[1]
        self.save_settings()
        token = adjustment_for(self.settings, self.photo, 'token')
        item = adjustment_for(self.settings, self.photo, 'item')
        self.assertEqual((token.zoom, token.x), (130, 0))
        self.assertEqual((item.layout, item.rotation, item.zoom, item.flip_horizontal),
                         (ITEM_MODES[1], 22, 100, False))
        self.assertEqual(token_cli.automatic_token(self.photo)[0], 'created')
        self.assertEqual(token_cli.automatic_item(self.photo)[0], 'created')
        for invalid in ('jpg', None, {}, '../png'):
            self.assertEqual(output_format(invalid), 'webp')
            with self.assertRaises(ValueError):
                output_format(invalid, strict=True)
        self.settings['ext'] = 'jpg'
        self.save_settings()
        with self.assertRaises(ValueError):
            token_cli.automatic_token(self.photo)
        self.assertEqual(output_format('PNG'), 'png')

    def test_automatic_corrupt_png_is_failure_not_successful_skip(self):
        self.settings['ext'] = 'png'
        self.settings['convert_original'] = False
        self.save_settings()
        state, output = token_cli.automatic_token(self.photo)
        self.assertEqual(state, 'created')
        data = output.read_bytes()
        import struct
        pos = 8
        while data[pos+4:pos+8] != b'IDAT':
            pos += 12 + struct.unpack('>I', data[pos:pos+4])[0]
        output.write_bytes(data[:pos+9])
        damaged = output.read_bytes()
        from token_engine import read_export_owner
        self.assertIsNotNone(read_export_owner(output))
        with self.assertRaisesRegex(RuntimeError, 'повреждено'):
            token_cli.automatic_token(self.photo)
        with patch.object(token_cli, 'notify_user') as notify:
            self.assertEqual(token_cli.run_auto(self.photo), 1)
            notify.assert_called_once()
            self.assertTrue(notify.call_args.kwargs['error'])
        self.assertEqual(output.read_bytes(), damaged)
        self.settings['skip_exists'] = False
        self.save_settings()
        self.assertEqual(token_cli.automatic_token(self.photo), ('created', output))
        with Image.open(output) as image:
            image.load()

    def test_conversion_warning_preserves_success(self):
        with patch('token_engine.save_original_webp', side_effect=OSError('conversion failed')):
            for action in (token_cli.automatic_token, token_cli.automatic_item):
                state, dest = action(self.photo)
                self.assertEqual(state, 'created_with_warning')
                self.assertTrue(dest.is_file())
                self.assertEqual(action(self.photo)[0], 'skipped_with_warning')

    def test_context_command_quotes_file_paths_and_mode(self):
        script = self.base / 'sendto_entry.py'
        args = context_menu.shortcut_arguments(script, '--open')
        self.assertEqual(args, f'"{script}" --open')
        self.assertNotIn('cmd.exe', args)
        with self.assertRaises(ValueError):
            context_menu.shortcut_arguments(script, '--erase')

    def test_gui_open_selects_user_target(self):
        instance = SimpleNamespace(run=lambda: None)
        with patch.object(tokenforge, 'TokenForge', return_value=instance) as mock:
            self.assertEqual(tokenforge.main(['--open', str(self.photo)]), 0)
            self.assertEqual(mock.call_args.kwargs['startup_file'], self.photo)

    def test_auto_routes_without_starting_gui(self):
        with patch('token_cli.run_auto', return_value=0) as auto, patch.object(tokenforge, 'TokenForge') as gui:
            self.assertEqual(tokenforge.main(['--auto', str(self.photo)]), 0)
            auto.assert_called_once_with(str(self.photo), item=False)
            gui.assert_not_called()

    def test_remote_or_unknown_model_is_rejected_before_io(self):
        self.settings['model'] = 'remote'
        self.save_settings()
        for action in (token_cli.automatic_token, token_cli.automatic_item):
            with self.assertRaisesRegex(ValueError, 'локальная модель'):
                action(self.photo)
        self.assertEqual(list(self.art_folder.iterdir()), [self.photo])

    def test_invalid_settings_prevent_automatic_publication(self):
        settings = self.pref / 'settings.json'
        for value in ('null', '[]', '{invalid'):
            settings.write_text(value, encoding='utf-8')
            for action in (token_cli.automatic_token, token_cli.automatic_item):
                with self.assertRaises(ValueError):
                    action(self.photo)
        self.assertEqual(list(self.art_folder.iterdir()), [self.photo])

    def test_invalid_file_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            token_cli.automatic_token(self.art_folder / 'нет.png')
        text = self.art_folder / 'bad.txt'
        text.write_text('hello')
        with self.assertRaises(ValueError):
            token_cli.automatic_token(text)


class FakeRegistry:
    HKEY_CURRENT_USER = 1234
    HKEY_CLASSES_ROOT = 5678
    HKEY_LOCAL_MACHINE = 7890
    KEY_WRITE = 1
    KEY_READ = 2
    REG_SZ = 1

    class Handle:
        def __init__(self, hive, path):
            self.hive, self.path = hive, path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def __init__(self, shadow=False):
        self.keys = {}
        self.shadow = shadow
        self.machine = {
            '*': {},
            r'*\shell': {},
            r'*\shell\Foreign': {'': ('Other command', self.REG_SZ)},
            r'*\shell\Foreign\command': {'': ('other.exe "%1"', self.REG_SZ)},
        }

    def _view(self, hive):
        if hive == self.HKEY_CURRENT_USER:
            return self.keys
        if hive == self.HKEY_LOCAL_MACHINE:
            return {'Software\\Classes\\' + p: v for p, v in self.machine.items()}
        assert hive == self.HKEY_CLASSES_ROOT
        prefix = 'Software\\Classes\\'
        user = {p[len(prefix):]: values for p, values in self.keys.items()
                if p.startswith(prefix)}
        result = dict(self.machine)
        if self.shadow and r'*\shell' in user:
            result = {p: v for p, v in result.items()
                      if p != r'*\shell' and not p.startswith('*\\shell\\')}
        result.update(user)
        return result

    def CreateKeyEx(self, hive, path, reserved, flags):
        assert hive == self.HKEY_CURRENT_USER
        parts = path.split('\\')
        for length in range(1, len(parts) + 1):
            self.keys.setdefault('\\'.join(parts[:length]), {})
        return self.Handle(hive, path)

    def OpenKey(self, hive, path, reserved=0, flags=2):
        if path not in self._view(hive):
            raise FileNotFoundError(path)
        return self.Handle(hive, path)

    def _children(self, key):
        prefix = key.path + '\\'
        return sorted(p[len(prefix):] for p in self._view(key.hive)
                      if p.startswith(prefix) and '\\' not in p[len(prefix):])

    def QueryInfoKey(self, key):
        return len(self._children(key)), len(self._view(key.hive)[key.path]), 0

    def EnumKey(self, key, index):
        return self._children(key)[index]

    def EnumValue(self, key, index):
        name, (value, kind) = list(self._view(key.hive)[key.path].items())[index]
        return name, value, kind

    def SetValueEx(self, key, name, reserved, kind, value):
        assert key.hive == self.HKEY_CURRENT_USER
        values = self.keys[key.path]
        actual = next((k for k in values if k.casefold() == name.casefold()), name)
        values[actual] = (value, kind)

    def QueryValueEx(self, key, name):
        values = self._view(key.hive)[key.path]
        for actual, value in values.items():
            if actual.casefold() == name.casefold():
                return value
        raise FileNotFoundError(name)

    def DeleteValue(self, key, name):
        assert key.hive == self.HKEY_CURRENT_USER
        values = self.keys[key.path]
        for actual in list(values):
            if actual.casefold() == name.casefold():
                del values[actual]
                return
        raise FileNotFoundError(name)

    def DeleteKey(self, hive, path):
        assert hive == self.HKEY_CURRENT_USER
        if path not in self.keys:
            raise FileNotFoundError(path)
        if self._children(self.Handle(hive, path)):
            raise OSError('Key still has children')
        del self.keys[path]


class SendToTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.folder = self.base / 'SendTo'
        self.folder.mkdir()
        self.exe = self.base / '.venv' / 'Scripts' / 'pythonw.exe'
        self.exe.parent.mkdir(parents=True)
        self.exe.touch()
        (self.base / 'tokenforge.py').touch()
        (self.base / 'sendto_entry.py').touch()
        self.reg = FakeRegistry(shadow=True)
        # Start with a clean machine branch, then introduce a foreign verb
        # before or after TokenForge setup.
        self.reg.machine = {'*': {}}
        for patcher in (
            patch.object(context_menu, 'MENU_LOCK', self.base / 'context-menu'),
            patch.object(context_menu, '_sendto_folder', return_value=self.folder),
            patch.object(context_menu, '_registry', return_value=self.reg),
            patch.object(context_menu, '_write_shortcut', side_effect=self.write_link),
            patch.object(context_menu, '_owned_shortcut', side_effect=self.owned_link),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def write_link(path, target, entry, flag):
        path.write_text(json.dumps({'owner': context_menu.LINK_OWNER, 'target': str(target),
                                    'args': f'"{entry}" {flag}'}, ensure_ascii=False), encoding='utf-8')

    @staticmethod
    def owned_link(path):
        try:
            return json.loads(path.read_text(encoding='utf-8')).get('owner') == context_menu.LINK_OWNER
        except (OSError, ValueError):
            return False

    def add_other_app(self):
        self.reg.machine[r'*\shell'] = {}
        self.reg.machine[r'*\shell\OtherApp'] = {'': ('Other application', self.reg.REG_SZ)}

    def test_other_app_installed_before_and_after_tokenforge(self):
        for foreign_first in (True, False):
            with self.subTest(foreign_first=foreign_first):
                self.reg.machine = {'*': {}}
                if foreign_first:
                    self.add_other_app()
                self.assertEqual(context_menu.install(self.base), [])
                installed = [json.loads(p.read_text(encoding='utf-8')) for p in self.folder.glob('*.lnk')]
                self.assertCountEqual([x['args'].split()[-1] for x in installed],
                                      ['--auto', '--open', '--auto-item', '--open-item'])
                for path, flag in context_menu.shortcut_paths(self.folder).items():
                    data = json.loads(path.read_text(encoding='utf-8'))
                    self.assertEqual(data['target'], str(self.exe))
                    self.assertEqual(data['args'], f'"{self.base / "sendto_entry.py"}" {flag}')
                if not foreign_first:
                    self.add_other_app()
                self.assertIn(r'*\shell\OtherApp', self.reg._view(self.reg.HKEY_CLASSES_ROOT))
                context_menu.remove()
                self.assertIn(r'*\shell\OtherApp', self.reg._view(self.reg.HKEY_CLASSES_ROOT))
                self.assertEqual(self.reg.keys, {})
                self.assertEqual(list(self.folder.iterdir()), [])

    def test_unrelated_shortcut_is_never_replaced_or_removed(self):
        path = next(iter(context_menu.shortcut_paths(self.folder)))
        path.write_bytes(b'foreign')
        with self.assertRaises(FileExistsError):
            context_menu.install(self.base)
        context_menu.remove()
        self.assertEqual(path.read_bytes(), b'foreign')

    def test_legacy_migration_removes_only_named_verbs(self):
        legacy = context_menu.ROOT + r'\TokenForge.Create\command'
        foreign = context_menu.ROOT + r'\OtherApp\command'
        legacy_ext = context_menu.LEGACY_ROOT + r'\.png\shell\TokenForge.Create\command'
        neighbor = context_menu.LEGACY_ROOT + r'\.png\shell\OtherApp\command'
        for path in (legacy, foreign, legacy_ext, neighbor):
            with self.reg.CreateKeyEx(self.reg.HKEY_CURRENT_USER, path, 0, self.reg.KEY_WRITE):
                pass
        context_menu.install(self.base)
        self.assertNotIn(legacy, self.reg.keys)
        self.assertNotIn(legacy.rsplit('\\', 1)[0], self.reg.keys)
        self.assertIn(foreign, self.reg.keys)
        self.assertIn(foreign.rsplit('\\', 1)[0], self.reg.keys)
        self.assertNotIn(legacy_ext, self.reg.keys)
        self.assertNotIn(legacy_ext.rsplit('\\', 1)[0], self.reg.keys)
        self.assertIn(neighbor, self.reg.keys)
        self.assertIn(neighbor.rsplit('\\', 1)[0], self.reg.keys)
        context_menu.remove()
        self.assertIn(foreign, self.reg.keys)
        self.assertIn(neighbor, self.reg.keys)

    def test_removal_preserves_replaced_shortcut(self):
        context_menu.install(self.base)
        path = next(iter(context_menu.shortcut_paths(self.folder)))
        original = context_menu._detach_expected
        def race(target, content, recovery):
            if target == path:
                target.write_bytes(b'foreign replacement')
            return original(target, content, recovery)
        with patch.object(context_menu, '_detach_expected', side_effect=race):
            with self.assertRaisesRegex(RuntimeError, 'изменился'):
                context_menu.remove()
        self.assertEqual(path.read_bytes(), b'foreign replacement')
        self.assertEqual(list(self.folder.iterdir()), [path])

    def test_reinstall_preserves_replaced_shortcut_and_rolls_back(self):
        context_menu.install(self.base)
        paths = list(context_menu.shortcut_paths(self.folder))
        originals = {p: p.read_bytes() for p in paths}
        replacement_base = self.base / 'New installation'
        replacement_base.mkdir()
        # The shortcut fixture uses the exe and script contents only through
        # their path; build a distinct valid install structure.
        import shutil
        for entry in self.base.iterdir():
            if entry.name in ('.venv', 'tokenforge.py', 'sendto_entry.py'):
                dest = replacement_base / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
        original = context_menu._detach_expected
        def race(target, content, recovery):
            if target == paths[1] and content == originals[target]:
                self.assertNotEqual(paths[0].read_bytes(), originals[paths[0]])
                target.write_bytes(b'foreign replacement')
            return original(target, content, recovery)
        with patch.object(context_menu, '_detach_expected', side_effect=race):
            with self.assertRaisesRegex(OSError, 'изменился'):
                context_menu.install(replacement_base)
        self.assertEqual(paths[1].read_bytes(), b'foreign replacement')
        for path in (paths[0], *paths[2:]):
            self.assertEqual(path.read_bytes(), originals[path])

    def test_removal_continues_after_inspection_failure(self):
        context_menu.install(self.base)
        path = next(iter(context_menu.shortcut_paths(self.folder)))
        with patch.object(context_menu, '_owned_shortcut', side_effect=[OSError('timeout'), True, True, True]):
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                context_menu.remove()
        self.assertEqual(list(self.folder.iterdir()), [path])

    def test_sendto_rejects_multiple_arguments(self):
        import sendto_entry
        with patch('token_cli.notify_user') as notify, patch('tokenforge.main') as app:
            self.assertEqual(sendto_entry.main(['--auto', 'a.png', 'b.png']), 2)
            notify.assert_called_once()
            app.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'Windows batch launcher')
class RealLauncherTests(unittest.TestCase):
    def test_run_bat_preserves_caller_working_directory(self):
        import os
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            program = root / 'Token Forge !'
            art = root / 'Art Folder'
            program.mkdir()
            art.mkdir()
            shutil.copy2(Path(__file__).parent / 'run.bat', program / 'run.bat')
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(program / '.venv')],
                           check=True, capture_output=True, timeout=45)
            (program / 'tokenforge.py').write_text(
                'import json, pathlib, sys\n'
                'print(json.dumps([str(pathlib.Path.cwd()), *sys.argv[1:]]))\n',
                encoding='utf-8')
            (art / 'hero one.png').touch()
            absolute = art / 'hero one.png'
            batch = program / 'run.bat'
            comspec = os.environ['COMSPEC']
            for args, expected in (('--open "hero one.png"', ['--open', 'hero one.png']),
                                   (f'--auto "{absolute}"', ['--auto', str(absolute)]),
                                   ('', [])):
                command = f'"{comspec}" /d /s /c ""{batch}" {args}"'
                result = subprocess.run(command, cwd=art, capture_output=True,
                                        text=True, timeout=30, check=True)
                actual = json.loads(result.stdout.splitlines()[-1])
                self.assertEqual(Path(actual[0]), art)
                self.assertEqual(actual[1:], expected)


@unittest.skipUnless(sys.platform == 'win32', 'Windows shortcut API')
class RealShortcutTests(unittest.TestCase):
    def test_create_read_and_remove_real_shortcuts(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            folder = base / 'SendTo'
            folder.mkdir()
            target = Path(sys.executable)
            entry = base / 'скрипт ! with spaces.py'
            entry.touch()
            context_menu._install_shortcuts(folder, target, entry)
            installed = [context_menu._shortcut_metadata(p) for p in folder.glob('*.lnk')]
            self.assertCountEqual([x['arguments'].split()[-1] for x in installed],
                                  ['--auto', '--open', '--auto-item', '--open-item'])
            for path, flag in context_menu.shortcut_paths(folder).items():
                self.assertTrue(path.is_file())
                info = context_menu._shortcut_metadata(path)
                self.assertEqual(info['description'], context_menu.LINK_OWNER, repr(info))
                self.assertEqual(Path(info['target']).resolve(), target.resolve())
                self.assertEqual(info['arguments'], context_menu.shortcut_arguments(entry, flag))
                self.assertTrue(context_menu._owned_shortcut(path))
            with patch.object(context_menu, '_sendto_folder', return_value=folder), \
                 patch.object(context_menu, 'MENU_LOCK', base / 'lock'), \
                 patch.object(context_menu, '_cleanup_previous', return_value=[]):
                context_menu.remove()
            self.assertEqual(list(folder.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
