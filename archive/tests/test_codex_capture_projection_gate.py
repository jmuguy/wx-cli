"""Check privacy through the actual capture consumer, rather than seeded master rows."""
import json

import pytest

from archive.tests.test_codex_collector_workflow import endpoint_fixture, export_fixture
from archive.tests.test_codex_projection_gate import assert_sentinel_absent
from archive.v3.util import canonical, sha256_hex


@pytest.mark.parametrize('body', [
    '<msg><appmsg><refermsg><title>CAPTUREHIDDENSENTINEL</title></refermsg><title>合法正文</title></appmsg></msg>',
    'visible-sender:\n<msg><appmsg><title>合法正文</title><refermsg><content>CAPTUREHIDDENSENTINEL</content></refermsg></appmsg></msg>',
])
def test_captured_content_derivation_cannot_promote_hidden_quote(tmp_path, endpoint_fixture, body):
    from archive.v3.projection import ProjectionBuilder, ProjectionService
    store, _, make_collector = endpoint_fixture
    export = export_fixture(tmp_path / 'export', body)
    path = export / 'records.jsonl'
    row = json.loads(path.read_text())
    row['local_type'] = 49
    row['raw']['local_type'] = 49
    row['real_sender_id'] = 2
    row['real_sender_name'] = 'visible-sender'
    row.pop('record_sha256')
    row['record_sha256'] = sha256_hex(canonical(row).encode())
    path.write_text(canonical(row) + '\n')
    make_collector().collect_session(export)
    store.acquire_writer_lock()
    builder = service = None
    try:
        with store.full_transaction():
            store.db.execute('UPDATE scope_grants SET query_allowed=1')
        policy = {'policy_version': 1, 'rules': {'account': {'talker': {
            'query': True, 'senders': {'mode': 'all'}, 'quotes': 'none',
            'tags': {'mode': 'none'}}}}}
        builder = ProjectionBuilder.init(store, policy)
        builder.build_and_publish()
        service = ProjectionService.open(store.binding.root / 'projection',
            projection_id='collector-fixture:projection', account='account',
            expected_epoch=1, allow_synthetic=True)
        assert_sentinel_absent(service, store.binding.root, 'CAPTUREHIDDENSENTINEL')
        assert service.search('talker', '合法正文')['count'] == 1
    finally:
        if service is not None:
            service.close()
        if builder is not None:
            builder.close()
        store.release_writer_lock()
