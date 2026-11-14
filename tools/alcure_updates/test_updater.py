import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error

from updater import Updater, locked


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


class Fixture(Updater):
    def validate(self, tree):
        if getattr(self, 'invalid', False):
            raise RuntimeError('candidate test failed')

    def verify_configuration(self, tree):
        if not getattr(self, 'config_valid', False):
            raise RuntimeError('configuration changed and is incompatible')

    def systemctl(self, *args, check=True):
        self.events.append(('systemctl', *args))
        return subprocess.CompletedProcess(args, 0 if self.active else 3, stdout='')

    def http(self, path, body=None):
        self.events.append((path, body))
        if path == '/health':
            return {'loaded': self.loaded, 'pack': {'filename': 'model.basalt'},
                    'control_features': ['maintenance_reservation']}
        if body == {'reserve': True}:
            if self.busy:
                raise urllib.error.HTTPError('', 409, 'busy', {}, None)
            return {'status': 'reserved', 'maintenance': True}
        return {}

    def wait_ready(self, expected_pack):
        self.events.append(('ready', str(self.current.resolve())))
        if self.fail_ready:
            self.fail_ready -= 1
            raise RuntimeError('load failed')

    def smoke(self):
        self.events.append(('smoke',))
        if getattr(self, 'fail_smoke', False):
            raise RuntimeError('inference failed')


class Updates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        upstream = self.root / 'upstream'
        upstream.mkdir()
        git(upstream, 'init', '-b', 'main')
        for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid')]:
            git(upstream, 'config', key, value)
        (upstream / 'core').write_text('base\n')
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-qm', 'base')
        self.upstream = upstream
        repo = self.root / 'repo'
        git(self.root, 'clone', str(upstream), str(repo))
        git(repo, 'remote', 'rename', 'origin', 'upstream')
        for key, value in [('user.name', 'Test'), ('user.email', 'test@example.invalid')]:
            git(repo, 'config', key, value)
        git(repo, 'switch', '-c', 'alcure-auto')
        (repo / 'local').write_text('preserved\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'local controls')
        self.baseline = git(repo, 'rev-parse', 'HEAD')
        old = self.root / 'old'
        old.mkdir()
        current = self.root / 'current'
        current.symlink_to(old)
        config = self.root / 'config.json'
        config.write_text('{}')
        self.u = Fixture({'repository': str(repo), 'state': str(self.root / 'state'),
                          'current': str(current), 'model_config': str(config),
                          'releases': str(self.root / 'releases'), 'gpu_lock': str(self.root / 'gpu.lock')})
        self.u.events = []
        self.u.active = self.u.loaded = True
        self.u.busy = False
        self.u.fail_ready = 0

    def changed_upstream(self):
        (self.upstream / 'new').write_text('new upstream behavior\n')
        git(self.upstream, 'add', '.')
        git(self.upstream, 'commit', '-qm', 'upstream release')

    def prepared(self):
        self.changed_upstream()
        self.u.prepare()
        return json.loads((self.u.state / 'pending.json').read_text())

    def test_merge_preserves_local_controls_and_does_not_touch_current(self):
        data = self.prepared()
        self.assertEqual((Path(data['tree']) / 'local').read_text(), 'preserved\n')
        self.assertTrue((Path(data['tree']) / 'new').exists())
        self.assertEqual(self.u.current.resolve(), self.root / 'old')
        self.assertEqual(git(self.u.repo, 'rev-parse', 'refs/heads/alcure-auto'), data['head'])

    def test_failed_validation_preserves_branch_and_active_tree(self):
        self.changed_upstream()
        self.u.invalid = True
        with self.assertRaisesRegex(RuntimeError, 'test failed'):
            self.u.prepare()
        self.assertEqual(git(self.u.repo, 'rev-parse', 'alcure-auto'), self.baseline)
        self.assertFalse((self.u.state / 'pending.json').exists())
        self.assertEqual(self.u.current.resolve(), self.root / 'old')
        self.u.invalid = False
        self.u.prepare()  # Held, no automatic retry of the same incompatible tree.
        self.assertFalse((self.u.state / 'pending.json').exists())

    def test_merge_conflict_is_retained_for_diagnosis(self):
        for tree, value in [(self.u.repo, 'local\n'), (self.upstream, 'upstream\n')]:
            (tree / 'core').write_text(value)
            git(tree, 'commit', '-qam', 'conflicting edit')
        before = git(self.u.repo, 'rev-parse', 'alcure-auto')
        with self.assertRaisesRegex(RuntimeError, 'conflicts'):
            self.u.prepare()
        self.assertEqual(git(self.u.repo, 'rev-parse', 'alcure-auto'), before)
        self.assertEqual(json.loads((self.u.state / 'conflicts.json').read_text())['files'], ['core'])
        self.assertEqual(self.u.current.resolve(), self.root / 'old')

    def test_running_request_defers_without_restart(self):
        self.prepared()
        self.u.busy = True
        self.u.activate()
        self.assertTrue((self.u.state / 'pending.json').exists())
        self.assertFalse(any('restart' in e for e in self.u.events))
        self.assertEqual(self.u.current.resolve(), self.root / 'old')

    def test_worker_gpu_lock_defers_activation(self):
        self.prepared()
        with locked(self.u.cfg['gpu_lock']):
            self.u.activate()
        self.assertEqual(self.u.events, [])

    def test_unloaded_motor_is_not_woken_up(self):
        self.prepared()
        self.u.loaded = False
        self.u.activate()
        self.assertFalse(any('restart' in e for e in self.u.events))
        self.assertTrue((self.u.state / 'pending.json').exists())

    def test_changed_config_is_rejected_before_reservation(self):
        self.prepared()
        self.u.config.write_text('{"changed":true}')
        with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
            self.u.activate()
        self.assertEqual(self.u.events, [])

    def test_changed_candidate_is_rejected(self):
        data = self.prepared()
        (Path(data['tree']) / 'local').write_text('edited')
        with self.assertRaisesRegex(RuntimeError, 'code changed'):
            self.u.activate()

    def test_compatible_pack_change_is_revalidated_automatically(self):
        data = self.prepared()
        self.u.config.write_text('{"changed":true}')
        self.u.config_valid = True
        self.u.activate()
        self.assertEqual(self.u.current.resolve(), Path(data['tree']))
        installed = json.loads((self.u.state / 'installed.json').read_text())
        self.assertEqual(installed['config_digest'], self.u.digest())

    def test_interrupted_activation_requires_inspection(self):
        self.prepared()
        (self.u.state / 'activating.json').write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'interrupted activation'):
            self.u.activate()
        self.assertEqual(self.u.events, [])

    def test_recovery_failure_preserves_the_transaction_record(self):
        self.prepared()
        self.u.fail_ready = 2
        with self.assertRaisesRegex(RuntimeError, 'load failed'):
            self.u.activate()
        self.assertTrue((self.u.state / 'activating.json').exists())
        self.assertEqual(self.u.current.resolve(), self.root / 'old')

    def test_successful_activation_preserves_previous_and_removes_pending(self):
        data = self.prepared()
        self.u.activate()
        self.assertEqual(self.u.current.resolve(), Path(data['tree']))
        self.assertEqual((self.root / 'previous').resolve(), self.root / 'old')
        self.assertFalse((self.u.state / 'pending.json').exists())
        self.assertEqual(sum('restart' in e for e in self.u.events), 1)

    def test_failed_start_restores_previous_and_does_not_loop(self):
        self.prepared()
        self.u.fail_ready = 1
        with self.assertRaisesRegex(RuntimeError, 'load failed'):
            self.u.activate()
        self.assertEqual(self.u.current.resolve(), self.root / 'old')
        self.assertEqual(sum('restart' in e for e in self.u.events), 2)
        self.assertFalse((self.u.state / 'pending.json').exists())
        self.assertTrue((self.u.state / 'rejected.json').exists())

    def test_stopped_motor_is_not_started(self):
        data = self.prepared()
        self.u.active = False
        self.u.activate()
        self.assertEqual(self.u.current.resolve(), Path(data['tree']))
        self.assertFalse(any('restart' in e for e in self.u.events))

    def test_failed_real_inference_also_restores_previous(self):
        self.prepared()
        self.u.fail_smoke = True
        with self.assertRaisesRegex(RuntimeError, 'inference failed'):
            self.u.activate()
        self.assertEqual(self.u.current.resolve(), self.root / 'old')
        self.assertFalse((self.u.state / 'pending.json').exists())


if __name__ == '__main__':
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main()
