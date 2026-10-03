"""Consumer-visible CLI/MCP scope and citation identity regressions."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ARCHIVE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ARCHIVE_DIR))
from config import load_config
from store import Archive


@pytest.fixture
def configured_archive(tmp_path):
    root = tmp_path / 'private'
    root.mkdir()
    binary = Path(sys.executable).resolve()
    (root / 'config.toml').write_text(
        '[archive]\naccount="synthetic"\nbinary=' + json.dumps(str(binary))
        + '\ntalkers=["allowed@chatroom"]\ninitial_since=1\n')
    export = tmp_path / 'source.json'
    sid = 9007199254740993
    export.write_text(json.dumps({
        'conversation': {'talker': 'allowed@chatroom', 'display_name': '合成授权群', 'message_count': 1},
        'items': [{'talker': 'allowed@chatroom', 'server_id': sid, 'local_id': 1,
                   'create_time': 1000, 'sort_seq': 1, 'sender': 'synthetic-sender',
                   'sender_display_name': '合成发送人', 'snippet': '周五提交合同',
                   'content': {'Text': '周五提交合同'}, 'msg_type': 1, 'sub_type': 0, 'status': 1}],
        'paging': {'offset': 0, 'returned': 1, 'has_more': False, 'total': 1},
        'stats': {'skipped': 0, 'scanned': 1},
    }, ensure_ascii=False))
    store = Archive(root)
    try:
        store.import_export(export, 'synthetic')
    finally:
        store.db.close()
    return root, sid


def run_mcp(root, calls):
    requests = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
                 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'}]
    requests.extend({'jsonrpc': '2.0', 'id': i + 1, 'method': 'tools/call',
                     'params': {'name': name, 'arguments': arguments}}
                    for i, (name, arguments) in enumerate(calls))
    proc = subprocess.run([sys.executable, str(ARCHIVE_DIR / 'wechat_archive.py'),
                           '--root', str(root), 'mcp'],
                          input='\n'.join(json.dumps(r) for r in requests) + '\n',
                          text=True, capture_output=True, check=True)
    return [json.loads(line) for line in proc.stdout.splitlines()][1:]


def test_mcp_cites_original_text_and_large_decimal_identity(configured_archive):
    root, sid = configured_archive
    replies = run_mcp(root, [
        ('search', {'query': '提交合同'}),
        ('get_context', {'talker': 'allowed@chatroom', 'server_id': str(sid), 'radius': 0}),
    ])
    found = json.loads(replies[0]['result']['content'][0]['text'])
    contextual = json.loads(replies[1]['result']['content'][0]['text'])
    assert found[0]['original_text'] == '周五提交合同'
    assert found[0]['sender_display_name'] == '合成发送人'
    assert found[0]['conversation_name'] == '合成授权群'
    assert contextual[0]['server_id'] == sid
    assert contextual[0]['original_text'] == found[0]['original_text']


@pytest.mark.parametrize('name,arguments', [
    ('get_context', {'talker': 'allowed@chatroom', 'server_id': '9223372036854775808'}),
    ('get_context', {'talker': 'allowed@chatroom', 'server_id': 9223372036854775808}),
    ('search', {'query': '合同', 'since': 9223372036854775808}),
    ('search', {'query': '合同', 'until': 9223372036854775808}),
])
def test_mcp_out_of_range_integer_does_not_kill_later_queries(configured_archive, name, arguments):
    root, sid = configured_archive
    replies = run_mcp(root, [
        (name, arguments),
        ('search', {'query': '提交合同'}),
    ])
    assert replies[0]['result']['isError']
    found = json.loads(replies[1]['result']['content'][0]['text'])
    assert found[0]['server_id'] == sid
    assert found[0]['original_text'] == '周五提交合同'

def test_mcp_large_ids_require_lossless_decimal_strings(configured_archive):
    root, sid = configured_archive
    replies = run_mcp(root, [
        ('get_context', {'talker': 'allowed@chatroom', 'server_id': sid, 'radius': 0}),
        ('get_context', {'talker': 'allowed@chatroom', 'server_id': str(sid), 'radius': 0}),
    ])
    assert replies[0]['result'].get('isError') is True
    contextual = json.loads(replies[1]['result']['content'][0]['text'])
    assert contextual[0]['server_id'] == sid
    assert contextual[0]['message_id'] == str(sid)
    assert contextual[0]['original_text'] == '周五提交合同'



def test_mcp_scope_cannot_be_overridden_and_write_tools_do_not_exist(configured_archive):
    root, _ = configured_archive
    replies = run_mcp(root, [
        ('search', {'query': '合同', 'account': 'another'}),
        ('search', {'query': '合同', 'talker': 'forbidden@chatroom'}),
        ('import', {'file': 'anything'}),
    ])
    assert all(reply['result']['isError'] for reply in replies)
    connection = sqlite3.connect(root / 'archive.sqlite3')
    try:
        assert connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    finally:
        connection.close()


def test_missing_configuration_does_not_create_archive(tmp_path):
    root = tmp_path / 'does-not-exist'
    proc = subprocess.run([sys.executable, str(ARCHIVE_DIR / 'wechat_archive.py'),
                           '--root', str(root), 'mcp'], input='', text=True, capture_output=True)
    assert proc.returncode != 0
    assert not root.exists()


def test_configuration_rejects_empty_scope_and_boolean_time(tmp_path):
    path = tmp_path / 'config.toml'
    for fields in ('talkers=[]\ninitial_since=1',
                   'talkers=["allowed"]\ninitial_since=true'):
        path.write_text('[archive]\naccount="synthetic"\nbinary="/bin/true"\n' + fields)
        with pytest.raises(ValueError):
            load_config(tmp_path)
