"""Independent privacy checks against the actual projection database and FTS."""
import json

import pytest

from archive.v3.guard import SyntheticGuard
from archive.v3.master import MasterStore
from archive.v3.util import canonical


@pytest.fixture
def projected(tmp_path):
    from archive.v3.projection import ProjectionBuilder, ProjectionService
    root = tmp_path / 'archive'
    root.mkdir(mode=0o700)
    SyntheticGuard.write_marker(root, 'privacy-fixture', 1, 0)
    binding = SyntheticGuard('privacy-fixture').verify(root)
    store = MasterStore.initialize(binding, 'privacy-fixture',
        {'account': {'talker': {'capture': True, 'query': True}}})
    store.acquire_writer_lock()
    policy = {'policy_version': 1, 'rules': {'account': {'talker': {
        'query': True, 'senders': {'mode': 'allowlist', 'allow': ['visible-sender']},
        'quotes': 'text_only', 'tags': {'mode': 'allowlist', 'allow': ['visible-tag']}}}}}
    def make(body, structured=None, refs=None, msg_type=1):
        with store.full_transaction() as tx:
            uid = store.next_msg_uid()
            store.db.execute('INSERT INTO messages(msg_uid,account,talker,create_time,sort_seq,msg_type,'
                'sender_id,content_text,content_json,media_refs_json,provenance,first_commit_seq,'
                'last_content_or_presence_commit_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (uid, 'account', 'talker', 100, uid, msg_type, 'visible-sender', body,
                 canonical(structured or {}), canonical(refs or []), 'capture', tx.commit_seq, tx.commit_seq))
        builder = ProjectionBuilder.init(store, policy)
        builder.build_and_publish()
        service = ProjectionService.open(root / 'projection', projection_id='privacy-fixture:projection',
            account='account', expected_epoch=1, allow_synthetic=True)
        return builder, service
    try:
        yield make, root
    finally:
        store.release_writer_lock()
        store.close()
        binding.close()


def assert_sentinel_absent(service, root, sentinel):
    assert service.search('talker', sentinel)['count'] == 0, 'hidden text entered FTS'
    assert sentinel not in canonical(service.messages('talker')), 'hidden text entered query payload'
    for db in (root / 'projection').glob('p-g*.db'):
        assert sentinel.encode() not in db.read_bytes(), 'hidden text persisted in query-readable database'


def test_raw_xml_quote_is_not_trusted_as_body_only_text(projected):
    make, root = projected
    secret = 'XMLHIDDENSENTINEL'
    body = '<msg><appmsg><title>可见标题</title><refermsg><fromusr>hidden-sender</fromusr>' \
           '<content>' + secret + '</content></refermsg></appmsg></msg>'
    builder, service = make(body, msg_type=49)
    try:
        assert_sentinel_absent(service, root, secret)
    finally:
        service.close(); builder.close()


def test_hidden_sender_quote_is_filtered_even_when_outer_sender_visible(projected):
    make, root = projected
    secret = 'QUOTESENDERSENTINEL'
    structured = {'text': '合法正文', 'quote': {'sender_id': 'hidden-sender', 'text': secret}}
    builder, service = make('合法正文', structured)
    try:
        assert_sentinel_absent(service, root, secret)
        assert service.search('talker', '合法正文')['count'] == 1
    finally:
        service.close(); builder.close()


def test_media_handle_cannot_expose_source_path(projected):
    make, root = projected
    secret = 'SOURCEPATHSENTINEL'
    ref = {'ref_key': 'file:/private/writer/' + secret + '.pdf', 'kind': 'file', 'present': False}
    builder, service = make('合法正文', refs=[ref])
    try:
        assert_sentinel_absent(service, root, secret)
    finally:
        service.close(); builder.close()


@pytest.mark.parametrize('body', [
    '<msg><appmsg><refermsg><title>NESTEDTITLESENTINEL</title></refermsg><title>合法正文</title></appmsg></msg>',
    'visible-sender:\n<msg><appmsg><title>合法正文</title><refermsg><content>NESTEDTITLESENTINEL</content></refermsg></appmsg></msg>',
])
def test_xml_whitelist_must_validate_ancestry_and_sender_prefix(projected, body):
    make, root = projected
    builder, service = make(body, msg_type=49)
    try:
        assert_sentinel_absent(service, root, 'NESTEDTITLESENTINEL')
    finally:
        service.close(); builder.close()


@pytest.mark.parametrize('body', [
    '<msg><appmsg><title>合法正文<refermsg><content>INNERQUOTESENTINEL</content></refermsg></title></appmsg></msg>',
    '<msg><appmsg><refermsg><foo></appmsg><title>INNERQUOTESENTINEL</title></foo></refermsg></appmsg></msg>',
])
def test_xml_tag_stripping_and_unbalanced_closes_cannot_expose_nested_payload(projected, body):
    make, root = projected
    builder, service = make(body, msg_type=49)
    try:
        assert_sentinel_absent(service, root, 'INNERQUOTESENTINEL')
    finally:
        service.close(); builder.close()


def test_tightened_policy_blocks_fresh_and_stateless_readers_before_publication(projected):
    import copy
    from archive.v3.projection import ProjectionError, ProjectionService
    make, root = projected
    builder, service = make('REVOKEDPOLICYSENTINEL')
    old = service.open_session()
    try:
        policy = copy.deepcopy(builder._read_policy())
        policy['policy_version'] += 1
        policy['rules']['account']['talker']['senders'] = {'mode': 'allowlist', 'allow': []}
        builder.set_policy(policy)
        for call in [lambda: old.messages('talker'), lambda: service.messages('talker'),
                     lambda: service.search('talker', 'REVOKEDPOLICYSENTINEL'),
                     lambda: service.sessions()]:
            with pytest.raises(ProjectionError):
                call()
        # A new session must not reset the stale token and gain access to the
        # obsolete projection while a replacement is incomplete or failed.
        with pytest.raises(ProjectionError):
            fresh = ProjectionService.open(root / 'projection', projection_id='privacy-fixture:projection',
                account='account', expected_epoch=1, allow_synthetic=True)
            try:
                fresh.messages('talker')
            finally:
                fresh.close()
        builder.build_and_publish()
        assert service.messages('talker')['count'] == 0
        assert service.search('talker', 'REVOKEDPOLICYSENTINEL')['count'] == 0
    finally:
        old.close(); service.close(); builder.close()


def test_changed_policy_requires_new_version_and_preserves_old_policy_on_rejection(projected):
    import copy
    from archive.v3.projection import ProjectionError
    make, root = projected
    builder, service = make('visible safe body')
    try:
        original = (root / 'projection' / 'policy' / 'policy.json').read_bytes()
        tightened = copy.deepcopy(builder._read_policy())
        tightened['rules']['account']['talker']['senders'] = {'mode': 'allowlist', 'allow': []}
        with pytest.raises(ProjectionError):
            builder.set_policy(tightened)
        assert (root / 'projection' / 'policy' / 'policy.json').read_bytes() == original
        unchanged = copy.deepcopy(builder._read_policy())
        builder.set_policy(unchanged)
        assert service.messages('talker')['count'] == 1
    finally:
        service.close(); builder.close()
