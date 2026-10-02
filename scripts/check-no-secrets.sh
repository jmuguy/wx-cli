#!/bin/sh
# Scan the working tree and staged blobs; never print matched secret bytes.
set -eu
root=${1:-$(git rev-parse --show-toplevel)}
exec python3 - "$root" <<'PY'
import hashlib
from pathlib import Path
import re
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
# Exact upstream synthetic fixture lines only, not whole-file exclusions.
fixtures = {
    'crates/wx-cli/tests/ignore-filters.rs': {'7683050a485b41ea1a7823cc384eacf248238492'},
    'crates/wx-cli/tests/serve-media.rs': {'7683050a485b41ea1a7823cc384eacf248238492'},
    'crates/wx-cli/tests/server_manager_cli.rs': {'7683050a485b41ea1a7823cc384eacf248238492'},
    'crates/wx-keychain/src/mach_vm/pattern.rs': {'c34be99e6c2bdb341c3e57e0171fba063e75965b'},
    'crates/wx-keychain/src/store.rs': {'427262d85a6fbe70a1071b61f8119f53b1b92678'},
}
hex_key = re.compile(rb'(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])')
manual_key = re.compile(rb'\bkey\s+set\s+(?:\S+\s+)?[0-9a-fA-F]{16,}\b')
checksum = re.compile(rb'checksum = "[0-9a-f]{64}"')

def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args])

def scan(name, content, surface):
    failures = 0
    for number, line in enumerate(content.splitlines(), 1):
        if not (hex_key.search(line) or manual_key.search(line)):
            continue
        if name == 'Cargo.lock' and checksum.fullmatch(line):
            continue
        if hashlib.sha1(line).hexdigest() in fixtures.get(name, set()):
            continue
        print(f'{surface}:{name}:{number}: possible secret (value withheld)', file=sys.stderr)
        failures += 1
    return failures

failures = 0
names = set(git('ls-files', '-co', '--exclude-standard', '-z').decode().split('\0')) - {''}
for name in sorted(names):
    path = root / name
    if path.is_symlink():
        print(f'working:{name}: symlink cannot be safely scanned', file=sys.stderr)
        failures += 1
    elif path.is_file():
        failures += scan(name, path.read_bytes(), 'working')
for entry in git('ls-files', '--stage', '-z').decode().split('\0'):
    if not entry:
        continue
    metadata, name = entry.split('\t', 1)
    mode, oid, stage = metadata.split()
    if mode == '160000':
        continue
    failures += scan(name, git('cat-file', 'blob', oid), f'index-{stage}')
if failures:
    print(f'FAIL: {failures} possible secret location(s); no values printed', file=sys.stderr)
    sys.exit(1)
print('PASS: working tree and index contain no unapproved key patterns')
PY
