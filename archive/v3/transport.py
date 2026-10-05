"""Transports: local subprocess and forced-command SSH stdio.

Both speak the same JSON-lines wire protocol over the child's stdin/stdout.
SshTransport runs `ssh <host> -- <forced command>`: in production the server's
authorized_keys forced command ignores the client-sent command, so sending it
is harmless and keeps the same code testable locally. `ssh_binary` is
injectable so tests can point at a stub that records argv and bridges to a
local endpoint — no real NAS is ever contacted by this codebase's tests.
"""
from __future__ import annotations

import os
import subprocess
import time

from .errors import ProtocolError
from .wire import MAX_FRAME_BYTES, decode_frame, encode_frame


class LineTransport:
    """One request per line, responses matched by id (strictly sequential)."""

    def __init__(self, argv, *, env=None):
        self.argv = list(argv)
        self._proc = subprocess.Popen(
            self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)

    # ── low-level line I/O ───────────────────────────────────────────
    def _send(self, frame: dict):
        try:
            self._proc.stdin.write(encode_frame(frame))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProtocolError('transport_closed', f'endpoint pipe closed: {exc}')

    def _recv(self):
        # Bound the read itself, not only decode_frame afterwards.  A
        # malicious or broken peer can otherwise force readline() to buffer
        # an arbitrarily large line before the frame-size check runs.
        line = self._proc.stdout.readline(MAX_FRAME_BYTES + 1)
        if not line:
            stderr = b''
            try:
                stderr = self._proc.stderr.read() or b''
            except OSError:
                pass
            raise ProtocolError(
                'transport_closed',
                'endpoint exited before responding',
                {'stderr_tail': stderr[-400:].decode('utf-8', 'replace')})
        if len(line) > MAX_FRAME_BYTES:
            raise ProtocolError('frame_too_large',
                                'inbound frame exceeds limit')
        return decode_frame(line)

    # ── request/response ─────────────────────────────────────────────
    def request(self, frame: dict) -> dict:
        self._send(frame)
        while True:
            resp = self._recv()
            if resp.get('id') == frame.get('id'):
                if resp.get('v') is not None and resp.get('ok') is None:
                    raise ProtocolError('frame_malformed', 'response lacks ok flag')
                return resp
            # stray line (e.g. endpoint banner): tolerate but bounded
            if not isinstance(resp.get('id'), int):
                raise ProtocolError('frame_malformed', 'unmatched response line')

    @property
    def alive(self):
        return self._proc.poll() is None

    def close(self):
        for stream in (self._proc.stdin,):
            try:
                stream.close()
            except OSError:
                pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def stderr_dump(self, limit=2000):
        try:
            self._proc.stderr.flush()
        except Exception:
            pass
        return b''


class LocalSubprocessTransport(LineTransport):
    """Local endpoint subprocess — synthetic E2E and local lzc deployment."""

    def __init__(self, endpoint_argv, *, env=None):
        super().__init__(endpoint_argv, env=env)


class SshTransport(LineTransport):
    """forced-command SSH transport. `remote_command` is what the server's
    forced command runs; BatchMode never prompts; no PTY (stdio protocol)."""

    def __init__(self, host, remote_command, *, ssh_binary='ssh',
                 ssh_config=None, connect_timeout=10, env=None):
        argv = [ssh_binary,
                '-o', 'BatchMode=yes',
                '-o', f'ConnectTimeout={connect_timeout}',
                '-o', 'StrictHostKeyChecking=yes',
                '-T', '--', host, remote_command]
        if ssh_config:
            argv[1:1] = ['-F', ssh_config]
        self.host = host
        self.remote_command = remote_command
        super().__init__(argv, env=env)


def with_retry(fn, *, attempts=3, base_delay=0.05, retry_codes=frozenset(),
               sleep=time.sleep):
    """Retry helper for idempotent ops only — safe BECAUSE batch_id/content
    addressing makes replays no-ops. Non-idempotent transport failures
    propagate to the caller for state recovery via batch_status."""
    last = None
    for attempt in range(attempts):
        try:
            return fn()
        except ProtocolError as exc:
            last = exc
            if exc.code not in retry_codes:
                raise
            if attempt == attempts - 1:
                raise
            sleep(base_delay * (2 ** attempt))
    raise last
