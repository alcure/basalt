#!/usr/bin/env python3
"""Install the already validated Basalt automation, backing up existing files first."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--storage', type=Path, required=True, help='Existing Basalt storage with current and models')
parser.add_argument('--repository', type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument('--python', default=sys.executable, help='Interpreter used to create candidate venvs')
parser.add_argument('--jobs', type=int, default=6)
args = parser.parse_args()
HOME_DIR = Path.home()
AUTOMATION = Path(__file__).resolve().parent
REPO = args.repository.expanduser().resolve()
STORAGE = args.storage.expanduser().resolve()
if not (STORAGE / 'current').is_dir() or not (STORAGE / 'models').is_dir():
    parser.error('storage must contain an existing current release and models directory')
if args.jobs < 1:
    parser.error('jobs must be positive')
subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--git-dir'], check=True, capture_output=True)
if subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--verify', 'refs/heads/alcure-auto'],
                  capture_output=True).returncode:
    subprocess.run(['git', '-C', str(REPO), 'branch', 'alcure-auto', 'HEAD'], check=True)
if subprocess.run(['git', '-C', str(REPO), 'remote', 'get-url', 'upstream'],
                  capture_output=True).returncode:
    subprocess.run(['git', '-C', str(REPO), 'remote', 'add', 'upstream',
                    'https://github.com/jesdga95/basalt.git'], check=True)
BACKUP = STORAGE / 'update-backups' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
BACKUP.mkdir(parents=True, mode=0o700)
STATE = HOME_DIR / '.local/state/alcure-basalt-update'
STATE.mkdir(parents=True, exist_ok=True)
CONFIG = HOME_DIR / '.config/basalt/updates.json'


def backup(path):
    if path.exists() or path.is_symlink():
        saved = BACKUP / path.relative_to(HOME_DIR)
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)
        os.chmod(saved, 0o600)


def write(path, text, executable=False):
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.installing')
    temp.write_text(text)
    temp.chmod(0o755 if executable else 0o600)
    os.replace(temp, path)


for original in [HOME_DIR / '.config/basalt/config.json',
                 HOME_DIR / '.local/bin/alcure-basalt-start',
                 HOME_DIR / '.config/systemd/user/alcure-basalt.service',
                 HOME_DIR / '.config/systemd/user/alcure-basalt.service.d/10-reserva-ram.conf']:
    backup(original)

configuration = {
    'repository': str(REPO),
    'releases': str(STORAGE / 'auto-releases'),
    'state': str(STATE),
    'current': str(STORAGE / 'current'),
    'model_config': str(HOME_DIR / '.config/basalt/config.json'),
    'models': str(STORAGE / 'models'),
    'python': args.python,
    'jobs': args.jobs,
    'ready_timeout': 240,
    'gpu_lock': str(HOME_DIR / '.cache/alcuresys-worker/llm-gpu.lock'),
    'update_lock': str(STORAGE / '.update.lock'),
}
write(CONFIG, json.dumps(configuration, indent=2) + '\n')
for name, action in [('alcure-basalt-update', 'update'), ('alcure-basalt-activate', 'activate'),
                     ('basalt-update-status', 'status')]:
    wrapper = f'''#!/usr/bin/env bash
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:$PATH"
exec > >(tee -a "$HOME/.cache/alcure-basalt-update.log") 2>&1
echo "=== $(date -Is) {name}"
exec /usr/bin/python "{AUTOMATION / 'updater.py'}" {action} "$@"
'''
    write(HOME_DIR / '.local/bin' / name, wrapper, executable=True)

units = HOME_DIR / '.config/systemd/user'
write(units / 'alcure-basalt-update.service', '''[Unit]
Description=Build and test Basalt updates preserving Alcure controls
OnSuccess=alcure-basalt-activate.service

[Service]
Type=oneshot
ExecStart=%h/.local/bin/alcure-basalt-update
TimeoutStartSec=4h
Nice=10
IOSchedulingClass=idle
CPUWeight=10
''')
write(units / 'alcure-basalt-update.timer', '''[Unit]
Description=Check Basalt upstream every six hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
RandomizedDelaySec=5min
Persistent=true
Unit=alcure-basalt-update.service

[Install]
WantedBy=timers.target
''')
write(units / 'alcure-basalt-activate.service', '''[Unit]
Description=Activate a tested Basalt release when the GPU is idle

[Service]
Type=oneshot
ExecStart=%h/.local/bin/alcure-basalt-activate
TimeoutStartSec=12min
''')
write(units / 'alcure-basalt-activate.timer', '''[Unit]
Description=Retry pending Basalt activation every ten minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min
Unit=alcure-basalt-activate.service

[Install]
WantedBy=timers.target
''')
hook = AUTOMATION / 'update-basalt.hook'
destination = HOME_DIR / '.config/omarchy/hooks/post-update.d/update-basalt.hook'
backup(destination)
subprocess.run(['omarchy', 'hook', 'install', 'post-update', str(hook)], check=True)
subprocess.run(['systemd-analyze', '--user', 'verify',
                str(units / 'alcure-basalt-update.service'), str(units / 'alcure-basalt-update.timer'),
                str(units / 'alcure-basalt-activate.service'), str(units / 'alcure-basalt-activate.timer')], check=True)
subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
subprocess.run(['systemctl', '--user', 'enable', '--now',
                'alcure-basalt-update.timer', 'alcure-basalt-activate.timer'], check=True)
print('Installed. Previous files saved in:', BACKUP)
