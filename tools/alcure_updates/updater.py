#!/usr/bin/env python3
"""Prepare tested Basalt releases and activate them under the Worker's GPU lock."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_CONFIG = Path.home() / '.config/basalt/updates.json'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def point(link, target):
    link = Path(link)
    temporary = link.with_name(link.name + '.new')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


@contextlib.contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Updater:
    def __init__(self, config):
        self.cfg = config
        self.repo = Path(config['repository'])
        self.state = Path(config['state'])
        self.current = Path(config['current'])
        self.config = Path(config['model_config'])
        self.state.mkdir(parents=True, exist_ok=True)

    def run(self, args, cwd=None, timeout=7200, check=True, capture=False):
        return subprocess.run([str(x) for x in args], cwd=cwd, timeout=timeout,
                              check=check, text=True,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None)

    def git(self, *args, tree=None, check=True):
        return self.run(['git', '-C', tree or self.repo, *args], check=check,
                        capture=True, timeout=180).stdout.strip()

    def report(self, status, **details):
        record = dict(status=status, time=dt.datetime.now().astimezone().isoformat(), **details)
        atomic_json(self.state / 'status.json', record)
        print(json.dumps(record), flush=True)
        if status in ('failed', 'installed', 'rolled_back'):
            notifier = shutil.which('notify-send')
            if notifier:
                subprocess.run([notifier, 'Basalt: ' + status,
                                details.get('error', details.get('tree', 'See basalt-update-status'))],
                               timeout=10, check=False)

    def digest(self):
        return hashlib.sha256(self.config.read_bytes()).hexdigest()

    def http(self, path, body=None):
        raw = json.loads(self.config.read_text())
        server = raw.get('server', {})
        host = server.get('host', '127.0.0.1')
        if host in ('0.0.0.0', '::'):
            host = '127.0.0.1'
        if host not in ('localhost', '127.0.0.1', '::1'):
            raise RuntimeError('activation requires a loopback server')
        host = f'[{host}]' if ':' in host else host
        url = f'http://{host}:{server.get("port", 8080)}{path}'
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

    def prepare(self, retry=False):
        if (self.state / 'pending.json').exists():
            self.report('pending', reason='a tested release is waiting for activation')
            return
        print('Fetching upstream', flush=True)
        self.git('fetch', '--quiet', 'upstream', 'main')
        upstream = self.git('rev-parse', 'refs/remotes/upstream/main')
        baseline = self.git('rev-parse', 'refs/heads/alcure-auto')
        fingerprint = f'{upstream}:{baseline}:{self.digest()}'
        failed = self.state / 'failed.json'
        if failed.exists() and json.loads(failed.read_text()).get('fingerprint') == fingerprint and not retry:
            self.report('held', reason='same incompatible version; see failed.json and last.log')
            return
        if self.run(['git', '-C', self.repo, 'merge-base', '--is-ancestor', upstream, baseline],
                    check=False).returncode == 0:
            rejected = self.state / 'rejected.json'
            if rejected.exists() and json.loads(rejected.read_text()).get('head') == baseline:
                self.report('held', reason='candidate failed at runtime; previous installation restored')
            else:
                self.report('current', upstream=upstream, integration=baseline)
            return
        releases = Path(self.cfg['releases'])
        releases.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(releases).free < 15 * 1024**3:
            raise RuntimeError('less than 15 GiB free for a candidate build')
        tree = releases / (upstream[:12] + '-' + dt.datetime.now().strftime('%Y%m%d%H%M%S'))
        self.git('worktree', 'add', '--detach', str(tree), baseline)
        try:
            message = 'merge: integrate upstream ' + upstream[:12] + '\n\nCo-authored-by: Codex <noreply@openai.com>'
            merged = self.run(['git', '-C', tree, 'merge', '--no-ff', '-m', message, upstream],
                              check=False, capture=True)
            print(merged.stdout, flush=True)
            if merged.returncode:
                conflicts = self.git('diff', '--name-only', '--diff-filter=U', tree=tree)
                atomic_json(self.state / 'conflicts.json', {'upstream': upstream, 'files': conflicts.splitlines()})
                raise RuntimeError('upstream conflicts with local controls: ' + conflicts.replace('\n', ', '))
            self.validate(tree)
            head = self.git('rev-parse', 'HEAD', tree=tree)
            self.git('update-ref', 'refs/heads/alcure-auto', head, baseline)
            self.stage(tree, head, upstream)
            failed.unlink(missing_ok=True)
        except BaseException as error:
            atomic_json(failed, {'fingerprint': fingerprint, 'error': str(error), 'candidate': str(tree)})
            self.report('failed', error=str(error), candidate=str(tree))
            # Keep the isolated tree for diagnosis; never change the active release.
            raise

    def validate(self, tree):
        tree = Path(tree)
        self.run(['uv', 'venv', '--python', self.cfg['python'], tree / '.venv'])
        python = tree / '.venv/bin/python'
        self.run(['uv', 'pip', 'install', '--python', python, '-r', 'requirements-test.txt'], cwd=tree)
        self.run(['cmake', '-S', tree, '-B', tree / 'build', '-DCMAKE_BUILD_TYPE=Release',
                  '-DCMAKE_CUDA_ARCHITECTURES=120'])
        self.run(['nice', '-n', '10', 'cmake', '--build', tree / 'build', '-j', str(self.cfg.get('jobs', 6))])
        self.verify(tree)

    def verify(self, tree):
        tree = Path(tree)
        self.run([tree / '.venv/bin/python', '-m', 'unittest', 'discover', '-s', 'serve/tests', '-t', '.'], cwd=tree)
        self.verify_configuration(tree)

    def verify_configuration(self, tree):
        tree = Path(tree)
        self.run([tree / '.venv/bin/python', Path(__file__).with_name('verify_release.py'),
                  tree, self.config, self.cfg['models']], cwd=tree, timeout=120)

    def stage(self, tree, head, upstream):
        tree = Path(tree).resolve()
        if tree == self.current.resolve():
            raise RuntimeError('candidate is already current')
        record = {'tree': str(tree), 'head': head, 'upstream': upstream,
                  'config_digest': self.digest(), 'previous': str(self.current.resolve())}
        atomic_json(tree / '.alcure-release.json', record)
        atomic_json(self.state / 'pending.json', record)
        self.report('staged', **record)

    def systemctl(self, *args, check=True):
        return self.run(['systemctl', '--user', *args], timeout=180, check=check, capture=True)

    def wait_ready(self, expected_pack):
        deadline = time.monotonic() + self.cfg.get('ready_timeout', 240)
        while time.monotonic() < deadline:
            try:
                health = self.http('/health')
                if (health.get('loaded') and not health.get('loading') and not health.get('maintenance')
                        and 'maintenance_reservation' in health.get('control_features', [])
                        and health.get('pack') == expected_pack):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(2)
        raise RuntimeError('new motor did not become ready with the expected pack')

    def smoke(self):
        health = self.http('/health')
        messages = [{'role': 'user', 'content': 'Responda apenas OK.'}]
        counted = self.http('/v1/messages/count_tokens', {'model': health['model'], 'messages': messages})
        if not isinstance(counted.get('input_tokens'), int) or counted['input_tokens'] <= 0:
            raise RuntimeError('candidate failed real token counting')
        answer = self.http('/v1/chat/completions', {
            'model': health['model'], 'messages': messages, 'max_tokens': 32, 'temperature': 0,
            'chat_template_kwargs': {'enable_thinking': False}})
        choices = answer.get('choices', [])
        if not choices or not choices[0].get('message', {}).get('content'):
            raise RuntimeError('candidate failed real inference')
        print('real token counting and short inference passed', flush=True)

    def activate(self):
        pending = self.state / 'pending.json'
        if not pending.exists():
            return
        if (self.state / 'activating.json').exists():
            raise RuntimeError('interrupted activation recorded; inspect activating.json before retrying')
        data = json.loads(pending.read_text())
        tree = Path(data['tree'])
        if self.current.resolve() != Path(data['previous']):
            raise RuntimeError('current changed since staging; refusing activation')
        if self.git('rev-parse', 'HEAD', tree=tree) != data['head'] or self.git('diff', 'HEAD', tree=tree):
            raise RuntimeError('candidate code changed since validation')
        # The reservation protects direct HTTP clients; the GPU lock also protects the Worker and Llamarchy.
        try:
            with locked(self.cfg['gpu_lock']):
                digest = self.digest()
                if digest != data['config_digest']:
                    self.verify_configuration(tree)
                    if self.digest() != digest:
                        raise RuntimeError('model configuration changed during validation')
                    data['config_digest'] = digest
                    atomic_json(tree / '.alcure-release.json', data)
                    atomic_json(pending, data)
                self._activate_locked(data, pending)
        except BlockingIOError:
            self.report('deferred', reason='Worker or Llamarchy is using the GPU')

    def _activate_locked(self, data, pending):
        unit = 'alcure-basalt.service'
        active = self.systemctl('is-active', '--quiet', unit, check=False).returncode == 0
        if not active:
            # Leave startup to the normal user session; no surprise model allocation.
            self.promote(data, pending)
            return
        health = self.http('/health')
        if not health.get('loaded') or health.get('loading') or health.get('maintenance') or health.get('swap'):
            self.report('deferred', reason='motor is unloaded, loading, swapping or reserved')
            return
        expected_pack = health.get('pack')
        if not expected_pack or 'maintenance_reservation' not in health.get('control_features', []):
            raise RuntimeError('current motor cannot safely reserve maintenance')
        try:
            result = self.http('/v1/unload', {'reserve': True})
        except urllib.error.HTTPError as error:
            if error.code == 409:
                self.report('deferred', reason='motor has running or queued requests')
                return
            raise
        if result.get('status') != 'reserved' or result.get('maintenance') is not True:
            raise RuntimeError('motor did not confirm the maintenance reservation')
        switched = False
        recovered = False
        try:
            atomic_json(self.state / 'activating.json', data)
            point(self.current, data['tree'])
            switched = True
            self.systemctl('restart', unit)
            self.wait_ready(expected_pack)
            self.smoke()
            self.promote(data, pending)
            recovered = True
        except BaseException:
            if switched:
                point(self.current, data['previous'])
                self.systemctl('restart', unit)
                self.wait_ready(expected_pack)
                recovered = True
                self.report('rolled_back', candidate=data['tree'], restored=data['previous'])
                # Do not retry a runtime-incompatible candidate automatically.
                atomic_json(self.state / 'rejected.json', data)
                pending.unlink(missing_ok=True)
            else:
                self.http('/v1/unload', {'reserve': False})
                self.http('/v1/load', {})
                recovered = True
            raise
        finally:
            if recovered:
                (self.state / 'activating.json').unlink(missing_ok=True)

    def promote(self, data, pending):
        point(self.current, data['tree'])
        point(self.current.with_name('previous'), data['previous'])
        atomic_json(self.state / 'installed.json', data)
        pending.unlink(missing_ok=True)
        self.report('installed', **data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['update', 'activate', 'status'])
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--retry', action='store_true')
    args = parser.parse_args()
    updater = Updater(json.loads(args.config.read_text()))
    if args.action == 'status':
        for name in ('status', 'pending', 'installed', 'failed'):
            path = updater.state / (name + '.json')
            if path.exists():
                print(name + ': ' + path.read_text())
        return
    try:
        mount = updater.cfg.get('storage_mount')
        if mount and not os.path.ismount(mount):
            raise RuntimeError('Basalt storage is not mounted: ' + mount)
        # Also serialize against the old updater during the transition.
        with locked(updater.cfg['update_lock']):
            if (updater.state / 'activating.json').exists():
                raise RuntimeError('interrupted activation recorded; preserving recovery information')
            if args.action == 'update':
                updater.prepare(args.retry)
            else:
                updater.activate()
    except BlockingIOError:
        print('another update operation holds the lock', flush=True)
    except Exception as error:
        print(f'Basalt update held: {error}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
