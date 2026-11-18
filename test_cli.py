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

    def test_conversion_warning_preserves_success(self):
        with patch('token_engine.save_original_webp', side_effect=OSError('conversion failed')):
            for action in (token_cli.automatic_token, token_cli.automatic_item):
                state, dest = action(self.photo)
                self.assertEqual(state, 'created_with_warning')
                self.assertTrue(dest.is_file())
                self.assertEqual(action(self.photo)[0], 'skipped_with_warning')

    def test_context_command_quotes_file_paths_and_mode(self):
        exe = self.base / '.venv' / 'Scripts' / 'pythonw.exe'
        script = self.base / 'tokenforge.py'
        cmd = context_menu.command_for(exe, script, '--open')
        self.assertEqual(cmd, f'"{exe}" "{script}" --open "%1"')
        self.assertNotIn('cmd.exe', cmd)
        with self.assertRaises(ValueError):
            context_menu.command_for(exe, script, '--erase')

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


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        lock_patch = patch.object(context_menu, 'MENU_LOCK', self.base / 'context-menu')
        lock_patch.start()
        self.addCleanup(lock_patch.stop)
        self.exe = self.base / '.venv' / 'Scripts' / 'pythonw.exe'
        self.exe.parent.mkdir(parents=True)
        self.exe.touch()
        (self.base / 'tokenforge.py').touch()

    def _legacy(self, reg, name='TokenForge.Create', ext='.png'):
        path = context_menu.LEGACY_ROOT + '\\' + ext + '\\shell\\' + name
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path + r'\command', 0, reg.KEY_WRITE) as key:
            reg.SetValueEx(key, '', 0, reg.REG_SZ, 'legacy.exe "%1"')
        return path

    def test_install_and_remove_scoped_verbs(self):
        reg = FakeRegistry()
        # Existing shared parent: registration must preserve it and foreign verbs.
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, context_menu.ROOT, 0, reg.KEY_WRITE):
            pass
        old = self._legacy(reg)
        foreign = self._legacy(reg, name='ThirdParty', ext='.jpg')
        baseline = context_menu._foreign_shell(reg)
        with patch.object(context_menu, '_registry', return_value=reg), \
             patch.object(context_menu, '_notify_shell') as notify:
            context_menu.install(self.base)
            expected = {'TokenForge.Create': '--auto', 'TokenForge.Open': '--open',
                        'TokenForge.CreateItem': '--auto-item', 'TokenForge.OpenItem': '--open-item'}
            for verb, flag in expected.items():
                key = context_menu.ROOT + '\\' + verb
                expected_cmd = f'"{self.exe}" "{self.base / "tokenforge.py"}" {flag} "%1"'
                self.assertEqual(reg.keys[key + r'\command'][''][0], expected_cmd)
                self.assertEqual(reg.keys[key]['AppliesTo'][0],
                                 'System.FileExtension:=.png OR System.FileExtension:=.jpg OR '
                                 'System.FileExtension:=.jpeg OR System.FileExtension:=.webp OR '
                                 'System.FileExtension:=.bmp OR System.FileExtension:=.tif OR '
                                 'System.FileExtension:=.tiff')
            self.assertNotIn(old, reg.keys)
            self.assertIn(foreign, reg.keys)
            self.assertEqual(context_menu._foreign_shell(reg), baseline)
            context_menu.remove()
            context_menu.remove()  # uninstall is idempotent
            self.assertFalse(any(p.startswith(context_menu.ROOT + '\\') for p in reg.keys))
            self.assertIn(context_menu.ROOT, reg.keys)
            self.assertIn(foreign, reg.keys)
            self.assertEqual(context_menu._foreign_shell(reg), baseline)
            self.assertEqual(notify.call_count, 3)

    def test_registry_preflight_refuses_without_losing_old_verbs(self):
        reg = FakeRegistry(shadow=True)
        old = self._legacy(reg)
        before = {key: dict(values) for key, values in reg.keys.items()}
        baseline = context_menu._foreign_shell(reg)
        with patch.object(context_menu, '_registry', return_value=reg), \
             patch.object(context_menu, '_notify_shell'):
            with self.assertRaises(context_menu.RegistryConflictError):
                context_menu.install(self.base)
            self.assertIn(old, reg.keys)
            self.assertEqual(reg.keys, before)
            self.assertEqual(context_menu._foreign_shell(reg), baseline)
            context_menu.remove()
            self.assertIn(context_menu.LEGACY_ROOT, reg.keys)
            self.assertNotIn(old, reg.keys)

    def test_rollback_restores_existing_values_without_deleting_keys(self):
        reg = FakeRegistry()
        path = context_menu.ROOT + r'\TokenForge.Create'
        command = path + r'\command'
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE) as key:
            reg.SetValueEx(key, '', 0, reg.REG_SZ, 'Old label')
            reg.SetValueEx(key, 'ICON', 0, reg.REG_SZ, 'Old icon')
            reg.SetValueEx(key, 'Independent', 0, reg.REG_SZ, 'Preserve')
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, command, 0, reg.KEY_WRITE) as key:
            reg.SetValueEx(key, '', 0, reg.REG_SZ, 'old.exe "%1"')
        baseline = {name: dict(values) for name, values in reg.keys.items()}
        original_set, original_delete = reg.SetValueEx, reg.DeleteKey

        def fail_later(key, name, reserved, kind, value):
            if key.path.endswith('TokenForge.Open') and name == 'Icon':
                raise PermissionError('Cannot update Open icon')
            original_set(key, name, reserved, kind, value)

        def deny_existing_delete(hive, key):
            if key in baseline:
                raise PermissionError('Existing key cannot be deleted')
            original_delete(hive, key)

        with patch.object(context_menu, '_registry', return_value=reg), \
             patch.object(context_menu, '_notify_shell') as notify, \
             patch.object(reg, 'SetValueEx', side_effect=fail_later), \
             patch.object(reg, 'DeleteKey', side_effect=deny_existing_delete):
            with self.assertRaisesRegex(PermissionError, 'Cannot update Open icon'):
                context_menu.install(self.base)
        self.assertEqual(reg.keys, baseline)
        notify.assert_called_once()

    def test_final_merged_view_mismatch_rolls_back_completed_writes(self):
        reg = FakeRegistry()
        path = context_menu.ROOT + r'\TokenForge.Create'
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE) as key:
            reg.SetValueEx(key, 'ICON', 0, reg.REG_SZ, 'old')
        old = self._legacy(reg)
        baseline = {key: dict(values) for key, values in reg.keys.items()}
        merged = context_menu._foreign_shell(reg)

        def changed_view(registry):
            self.assertTrue(all(context_menu.ROOT + '\\' + name in registry.keys
                                for name in context_menu.ACTION_NAMES))
            return {}

        with patch.object(context_menu, '_registry', return_value=reg), \
             patch.object(context_menu, '_notify_shell') as notify, \
             patch.object(context_menu, '_check_safe_registration', return_value=merged), \
             patch.object(context_menu, '_foreign_shell', side_effect=changed_view):
            with self.assertRaises(context_menu.RegistryConflictError):
                context_menu.install(self.base)
        self.assertEqual(reg.keys, baseline)
        self.assertIn(old, reg.keys)
        notify.assert_called_once()

    def test_install_and_remove_use_same_lock(self):
        from contextlib import contextmanager
        calls = []
        active = []
        @contextmanager
        def lock(path, timeout):
            calls.append(path)
            active.append(path)
            try:
                yield
            finally:
                active.pop()

        def protected(*args):
            self.assertEqual(active, [context_menu.MENU_LOCK])
            return []

        with patch.object(context_menu, 'process_lock', side_effect=lock), \
             patch.object(context_menu, '_install', side_effect=protected), \
             patch.object(context_menu, '_remove', side_effect=protected):
            context_menu.install(self.base)
            context_menu.remove()
        self.assertEqual(calls, [context_menu.MENU_LOCK, context_menu.MENU_LOCK])
        self.assertEqual(active, [])

    def test_legacy_cleanup_failure_is_warning_and_keeps_new_commands(self):
        reg = FakeRegistry()
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, context_menu.ROOT, 0, reg.KEY_WRITE):
            pass
        old = self._legacy(reg)
        original = reg.DeleteKey

        def deny_legacy(hive, key):
            if key == old + r'\command':
                raise PermissionError('Legacy command protected')
            original(hive, key)

        with patch.object(context_menu, '_registry', return_value=reg), \
             patch.object(context_menu, '_notify_shell') as notify, \
             patch.object(reg, 'DeleteKey', side_effect=deny_legacy):
            warnings = context_menu.install(self.base)
        self.assertTrue(any('Legacy command protected' in warning for warning in warnings))
        self.assertIn(old, reg.keys)
        self.assertIn(context_menu.ROOT + r'\TokenForge.Create', reg.keys)
        notify.assert_called_once()

    def test_remove_leaves_preexisting_empty_shared_keys(self):
        reg = FakeRegistry()
        paths = [context_menu.ROOT, context_menu.LEGACY_ROOT + r'\.png\shell']
        for path in paths:
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE):
                pass
        baseline = dict(reg.keys)
        with patch.object(context_menu, '_registry', return_value=reg), \
             patch.object(context_menu, '_notify_shell'):
            context_menu.remove()
        self.assertEqual(reg.keys, baseline)

    @unittest.skipUnless(
        __import__('os').name == 'nt'
        and __import__('os').environ.get('GITHUB_ACTIONS') == 'true'
        and __import__('os').environ.get('TOKENFORGE_TEST_REAL_REGISTRY') == '1',
        'Real registry smoke test is opt-in on the disposable Windows CI runner')
    def test_real_windows_registry_preserves_foreign_verbs(self):
        reg = context_menu._registry()
        baseline = context_menu._foreign_shell(reg)
        owned_paths = [context_menu.ROOT + '\\' + name for name in context_menu.ACTION_NAMES]
        owned_paths += [context_menu.LEGACY_ROOT + '\\' + ext + '\\shell\\' + name
                        for ext in context_menu.EXTENSIONS for name in context_menu.ACTION_NAMES]
        if any(context_menu._snapshot_tree(reg, reg.HKEY_CURRENT_USER, path) for path in owned_paths):
            self.skipTest('Real registry fixture already has TokenForge registrations')
        installed = False
        try:
            try:
                warnings = context_menu.install(self.base)
                installed = True
            except context_menu.RegistryConflictError:
                self.assertEqual(context_menu._foreign_shell(reg), baseline)
                self.skipTest('Per-user static registration would shadow machine data')
            self.assertEqual(warnings, [])
            for name in context_menu.ACTION_NAMES:
                with reg.OpenKey(reg.HKEY_CURRENT_USER,
                                 context_menu.ROOT + '\\' + name) as key:
                    self.assertEqual(reg.QueryValueEx(key, 'AppliesTo')[0], context_menu.APPLIES_TO)
        finally:
            if installed:
                context_menu.remove()
        after = context_menu._foreign_shell(reg)
        self.assertTrue(all(after.get(path) == values for path, values in baseline.items()))


if __name__ == '__main__':
    unittest.main()
