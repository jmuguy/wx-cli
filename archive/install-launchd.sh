#!/bin/bash
# Install (but never load) the howie-wechat-archive launchd jobs.
#
# Usage: archive/install-launchd.sh <archive-root>
#
# Validates <archive-root>/config.toml plus the [archive] paths inside it,
# then renders the archive/launchd/*.plist templates into
# ~/Library/LaunchAgents with python3 + plistlib (no shell interpolation of
# user-controlled data) using mode 0600. Loading stays manual:
#   launchctl load ~/Library/LaunchAgents/com.howie.wechat-archive.<job>.plist
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <archive-root>" >&2
  exit 2
fi

ARCHIVE_ROOT=$1
TEMPLATES_DIR=$(cd "$(dirname "$0")" && pwd)/launchd

if [[ ! -f "$ARCHIVE_ROOT/config.toml" ]]; then
  echo "install-launchd: missing config: $ARCHIVE_ROOT/config.toml" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "install-launchd: python3 not found" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "install-launchd: requires python3 >= 3.11 (tomllib)" >&2
  exit 1
fi

python3 - "$ARCHIVE_ROOT" "$TEMPLATES_DIR" <<'PY'
import os
import plistlib
import sys
import shutil
import tomllib
from pathlib import Path

os.umask(0o077)
root = Path(sys.argv[1]).expanduser().resolve()
templates = Path(sys.argv[2]).resolve()

try:
    config = tomllib.loads((root / 'config.toml').read_text(encoding='utf-8'))
except tomllib.TOMLDecodeError as exc:
    sys.exit(f'install-launchd: config.toml is not valid TOML: {exc}')
archive_cfg = config.get('archive')
if not isinstance(archive_cfg, dict):
    sys.exit('install-launchd: config.toml missing [archive] section')

account = archive_cfg.get('account')
binary = Path(str(archive_cfg.get('binary') or ''))
talkers = archive_cfg.get('talkers')
if not isinstance(account, str) or not account:
    sys.exit('install-launchd: [archive] account missing or empty')
if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
    sys.exit('install-launchd: [archive] binary must be an absolute path '
             'to an executable wx binary')
if (not isinstance(talkers, list) or not talkers
        or not all(isinstance(t, str) and t for t in talkers)):
    sys.exit('install-launchd: [archive] talkers must be a non-empty opt-in list')

script = templates.parent / 'wechat_archive.py'
if not script.is_file():
    sys.exit(f'install-launchd: missing CLI script {script}')

placeholders = {
    '__PYTHON__': str(Path(shutil.which('python3')).absolute()),
    '__ARCHIVE_SCRIPT__': str(script),
    '__ARCHIVE_ROOT__': str(root),
}

launch_agents = Path.home() / 'Library' / 'LaunchAgents'
launch_agents.mkdir(parents=True, exist_ok=True, mode=0o700)

for template_path in sorted(templates.glob('*.plist')):
    with template_path.open('rb') as fh:
        job = plistlib.load(fh)
    job['ProgramArguments'] = [placeholders.get(arg, arg)
                               for arg in job.get('ProgramArguments', [])]
    for key in ('StandardOutPath', 'StandardErrorPath'):
        if key in job:
            relative_log = Path(job[key]).relative_to('__ARCHIVE_ROOT__')
            target = root / relative_log
            job[key] = str(target)
            # Pre-create with 0600 so launchd appends to private logs instead
            # of creating world-readable files with the default umask.
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(target.parent, 0o700)
            target.touch(exist_ok=True)
            os.chmod(target, 0o600)
    dest = launch_agents / f"{job['Label']}.plist"
    with dest.open('wb') as fh:
        plistlib.dump(job, fh)
    os.chmod(dest, 0o600)
    print(f'installed {dest} (NOT loaded)')

print('Load manually when ready, e.g.:')
print('  launchctl load ~/Library/LaunchAgents/com.howie.wechat-archive.discover.plist')
print('  launchctl load ~/Library/LaunchAgents/com.howie.wechat-archive.reconcile.plist')
PY
