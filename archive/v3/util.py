"""Small shared helpers (canonical JSON, hashing, durable file writes).

Durability rules enforced here for EVERY caller (util, guard, protocol):
- os.write may short-write: write_all() loops until every byte lands and
  treats a zero-byte write as a hard failure;
- every durable write uses a UNIQUE temp name (pid + random token) and removes
  it on failure, so a crashed process can never leave a stuck fixed-name temp
  blocking later writes;
- fsync failures ALWAYS propagate — no swallow, no "best effort". A durability
  layer that ignores persistence errors is a silent-data-loss layer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode('ascii'), validate=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def write_all(fd, data: bytes):
    """Write the COMPLETE buffer, looping over short writes. A zero-byte
    write means the fd can never accept data — hard failure."""
    view = memoryview(data)
    total = 0
    while total < len(view):
        written = os.write(fd, view[total:])
        if written <= 0:
            raise OSError(22, 'write_all: zero-byte write', None,
                          total, 'write returned 0')
        total += written
    return total


def unique_temp(name: str) -> str:
    """Unique sibling temp name; uniqueness prevents crashed-process leftovers
    from colliding with (and blocking) a later attempt."""
    return f'.{name}.tmp.{os.getpid()}.{secrets.token_hex(6)}'


def fsync_fd(fd):
    os.fsync(fd)


def fsync_path(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_durable(path, obj, mode=0o600):
    """Atomic durable write OUTSIDE the guard binding (client workspace files).
    Same rules as guard._Binding.write_file_atomic: full write, unique temp
    with cleanup, propagated fsync on file and directory."""
    path = os.fspath(path)
    directory = os.path.dirname(path) or '.'
    tmp = os.path.join(directory, unique_temp(os.path.basename(path)))
    data = canonical(obj).encode('utf-8') + b'\n'
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(directory)


def read_json(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return json.load(fh)


def identity_key(server_id, shard, shard_table, local_rowid):
    """Stable identity key per nas-contract v2 §5: s:<server_id> for rows with
    a server identity; l:<database>:<table>:<rowid> for local-only rows (the
    table is part of local identity — the same Msg table name exists in
    multiple message_N.db shards)."""
    if server_id is not None:
        return f's:{server_id}'
    return f'l:{shard}:{shard_table}:{local_rowid}'
