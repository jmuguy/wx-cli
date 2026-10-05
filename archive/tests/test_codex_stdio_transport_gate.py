"""Real subprocess and injected-SSH protocol checks over a published projection.

The SSH executable is a local stub: these tests never contact a server.
"""
import json
import sys
import tracemalloc

import pytest

from archive.tests.test_codex_projection_gate import projected
from archive.v3.transport import LocalSubprocessTransport, SshTransport
from archive.v3.wire import make_request


def test_oversize_wire_response_is_rejected_without_buffering_entire_line():
    from archive.v3.errors import ProtocolError
    from archive.v3.wire import MAX_FRAME_BYTES
    producer = 'import os\nfor i in range(64): os.write(1,b"x"*(1024*1024))\nos.write(1,b"\\n")\n'
    transport = LocalSubprocessTransport([sys.executable, '-c', producer])
    tracemalloc.start()
    try:
        with pytest.raises(ProtocolError) as failed:
            transport.request(make_request(1, 'ping'))
        assert failed.value.code == 'frame_too_large'
        _, peak = tracemalloc.get_traced_memory()
        assert peak < MAX_FRAME_BYTES * 4, 'wire reader buffered the entire 64 MiB invalid response'
    finally:
        tracemalloc.stop()
        transport.close()


@pytest.mark.parametrize('transport_kind', ['subprocess', 'ssh_stub'])
def test_query_stdio_uses_only_projection_and_denies_capture(tmp_path, projected, transport_kind):
    make, root = projected
    builder, service = make('合法正文 synthetic subprocess query')
    child = tmp_path / 'query_endpoint.py'
    child.write_text('''import os,sys
sys.path.insert(0, os.getcwd())
from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.projection import ProjectionService
from archive.v3.endpoint import Endpoint, serve_endpoint
from archive.v3.util import sha256_hex
def master_forbidden(*a, **kw):
    raise AssertionError('query endpoint attempted to open master')
MasterStore.__init__ = master_forbidden
root = sys.argv[1]
service = ProjectionService.open(root, projection_id='privacy-fixture:projection',
    account='account', expected_epoch=1, allow_synthetic=True)
binding = SyntheticGuard('privacy-fixture:projection').verify(root)
endpoint = Endpoint(binding, 'privacy-fixture', lambda: service)
endpoint.auth = {'roles': {'query': {'token_sha256': sha256_hex(b'synthetic-query-token')}}}
try:
    result = serve_endpoint(endpoint, sys.stdin.buffer, sys.stdout.buffer)
finally:
    service.close(); binding.close()
sys.exit(result)
''')
    argv = [sys.executable, str(child), str(root / 'projection')]
    transport = None
    try:
        if transport_kind == 'subprocess':
            transport = LocalSubprocessTransport(argv)
        else:
            stub = tmp_path / 'ssh-stub'
            stub.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                'assert sys.argv[-2:] == ["synthetic-host.invalid", "synthetic-forced-query"]\n'
                'assert "BatchMode=yes" in sys.argv and "StrictHostKeyChecking=yes" in sys.argv\n'
                'assert "-T" in sys.argv\n'
                'argv=' + repr(argv) + '\nos.execv(argv[0],argv)\n')
            stub.chmod(0o700)
            transport = SshTransport('synthetic-host.invalid', 'synthetic-forced-query', ssh_binary=str(stub))
        denied = transport.request(make_request(1, 'hello', role='collector', token='synthetic-query-token', archive_id='privacy-fixture'))
        assert not denied['ok']
        hello = transport.request(make_request(2, 'hello', role='query', token='synthetic-query-token', archive_id='privacy-fixture'))
        assert hello['ok'] and hello['result']['role'] == 'query'
        found = transport.request(make_request(3, 'query_search', talker='talker', match='合法正文'))
        assert found['ok'] and found['result']['count'] == 1
        prohibited = transport.request(make_request(4, 'begin_run', account='account', talker='talker', run_id='prohibited'))
        assert not prohibited['ok'] and prohibited['error']['code'] == 'op_not_allowed_for_role'
        assert 'raw_record_json' not in json.dumps(found)
    finally:
        if transport is not None:
            transport.close()
        service.close(); builder.close()


def test_oversize_request_closes_framing_session_without_processing_suffix():
    import io
    from archive.v3.endpoint import serve_endpoint
    from archive.v3.wire import MAX_FRAME_BYTES, encode_frame
    class EndpointProbe:
        fatal = None
        calls = []
        def handle_frame(self, frame):
            self.calls.append(frame)
            return {'id': frame['id'], 'ok': True}
    endpoint = EndpointProbe()
    # A valid request after the invalid line must never be reinterpreted as
    # a fresh request from the same authenticated framing session.
    source = io.BytesIO(b'x' * (MAX_FRAME_BYTES * 2) + b'\n' +
                        encode_frame(make_request(7, 'ping')))
    out = io.BytesIO()
    tracemalloc.start()
    try:
        code = serve_endpoint(endpoint, source, out)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert code != 0
    assert endpoint.calls == []
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == 1
    assert replies[0]['error']['code'] == 'frame_too_large'
    assert source.tell() <= MAX_FRAME_BYTES + 1
    assert peak < MAX_FRAME_BYTES * 4
