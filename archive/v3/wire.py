"""Wire protocol: versioned JSON-lines frames and role operation sets.

The line protocol is transport-independent by design: LocalSubprocessTransport
(synthetic E2E and local lzc deployment) and SshTransport (forced-command SSH)
carry identical bytes, and the same endpoint script serves both. Roles are
enforced server-side: a token authenticates exactly one role, a role sees
exactly its operation subset, unknown roles are refused at hello, and any op
before a successful hello is refused.
"""
from __future__ import annotations

import json

from . import PROTOCOL_VERSION
from .errors import ProtocolError

MAX_FRAME_BYTES = 8 * 1024 * 1024  # bounded frames: one chunk ≤ 1 MiB raw b64

ROLE_COLLECTOR = 'collector'
ROLE_QUERY = 'query'

ROLE_OPS = {
    ROLE_COLLECTOR: frozenset({
        'hello', 'ping', 'begin_run', 'run_status', 'get_observation',
        'observation_ids', 'begin_upload', 'upload_chunk', 'finish_upload',
        'commit_batch', 'batch_status', 'abort_batch',
    }),
    ROLE_QUERY: frozenset({
        'hello', 'ping', 'query_manifest', 'query_messages', 'query_search',
    }),
}


def encode_frame(frame: dict) -> bytes:
    line = json.dumps(frame, ensure_ascii=False, separators=(',', ':'))
    data = line.encode('utf-8') + b'\n'
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError('frame_too_large',
                            f'frame of {len(data)} bytes exceeds {MAX_FRAME_BYTES}')
    return data


def decode_frame(line: bytes) -> dict:
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError('frame_too_large', 'inbound frame exceeds limit')
    try:
        frame = json.loads(line.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError('frame_malformed', f'not valid JSON: {exc}')
    if not isinstance(frame, dict):
        raise ProtocolError('frame_malformed', 'frame must be a JSON object')
    return frame


def make_request(req_id, op, **fields):
    frame = {'v': PROTOCOL_VERSION, 'id': req_id, 'op': op}
    frame.update(fields)
    return frame


def make_response(req_id, result=None, error=None):
    frame = {'v': PROTOCOL_VERSION, 'id': req_id}
    if error is not None:
        frame['ok'] = False
        frame['error'] = error.to_json() if hasattr(error, 'to_json') else error
    else:
        frame['ok'] = True
        frame['result'] = {} if result is None else result
    return frame


def validate_request(frame):
    """Structural request validation → (id, op). Version checked strictly."""
    if frame.get('v') != PROTOCOL_VERSION:
        raise ProtocolError('protocol_version_mismatch',
                            f'wire version {frame.get("v")!r} != {PROTOCOL_VERSION}')
    req_id = frame.get('id')
    op = frame.get('op')
    if not isinstance(req_id, int) or not isinstance(op, str) or not op:
        raise ProtocolError('frame_malformed', 'request needs int id and op')
    return req_id, op
