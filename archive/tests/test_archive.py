import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'wechat_archive.py'
spec = importlib.util.spec_from_file_location('archive', MODULE)
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = a.Archive(self.root / 'private')
    def tearDown(self):
        self.store.db.close(); self.tmp.cleanup()
    def message(self, sid, text='收到', sender='alice', ts=1000):
        return dict(server_id=sid,talker='group@chatroom',create_time=ts,sort_seq=sid,
                    sender=sender,content={'Text':text},snippet=text,media_files=[])
    def fixture(self, items):
        data = dict(items=items,conversation=dict(talker='group@chatroom',message_count=len(items)),
                    stats=dict(skipped=0,shard_warnings=[]),
                    paging=dict(has_more=False,offset=0,returned=len(items)))
        return data
    def load(self, data, account='howiefire', bounds=None):
        p=self.root/'export.json'; p.write_text(json.dumps(data))
        return self.store.import_export(p,account,bounds=bounds)
    def test_same_second_identical_text_and_repeat_import(self):
        data=self.fixture([self.message(101),self.message(102,sender='bob')])
        self.load(data); self.load(data)
        self.assertEqual(len(self.store.search('收到','howiefire')),2)
        self.assertEqual(len(self.store.context('howiefire','group@chatroom',101,10)),2)
    def test_accounts_are_separated_and_revisions_preserved(self):
        self.load(self.fixture([self.message(101)]))
        self.load(self.fixture([self.message(101,'changed')]))
        self.load(self.fixture([self.message(101)]),account='another')
        self.assertEqual(self.store.status()['messages'],2)
        self.assertEqual(self.store.db.execute('select count(*) from revisions').fetchone()[0],3)
    def test_invalid_id_warnings_pagination_and_missing_assets_fail_closed(self):
        mutations=[lambda d:d['items'][0].update(server_id=0),
                   lambda d:d['stats'].update(skipped=1),
                   lambda d:d['stats'].update(shard_warnings=[{'reason':'broken'}]),
                   lambda d:d['paging'].update(has_more=True),
                   lambda d:d['items'][0].update(media_files=['media/missing.jpg']),
                   lambda d:d['items'][0].update(media_files=['../escape']),
                   lambda d:d['items'][0].pop('content')]
        for change in mutations:
            d=self.fixture([self.message(101)]); change(d)
            with self.assertRaises((ValueError,KeyError)): self.load(d,bounds=(900,1100))
        self.assertEqual(self.store.status()['messages'],0)
        self.assertEqual(self.store.status()['checkpoints'],[])
    def test_historical_backfill_does_not_regress_checkpoint(self):
        self.load(self.fixture([self.message(101)]),bounds=(900,1100))
        self.load(self.fixture([self.message(102,ts=800)]),bounds=(700,900))
        self.assertEqual(self.store.status()['checkpoints'][0]['until_ts'],1100)
        self.assertEqual(self.store.status()['messages'],2)
    def test_media_and_source_survive_deleted_export(self):
        media=self.root/'media'; media.mkdir(); (media/'image.dat').write_bytes(b'fixture-media')
        d=self.fixture([self.message(101)]); d['items'][0]['media_files']=['media/image.dat']
        result=self.load(d); (self.root/'export.json').unlink(); (media/'image.dat').unlink()
        source=Path(self.store.search('收到','howiefire')[0]['source'])
        self.assertTrue(source.exists())
        self.assertEqual((source.parent/'media/image.dat').read_bytes(),b'fixture-media')
    def test_mcp_initialize_search_context(self):
        self.load(self.fixture([self.message(101)]))
        requests=[dict(jsonrpc='2.0',id=1,method='initialize',params={}),
                  dict(jsonrpc='2.0',method='notifications/initialized'),
                  dict(jsonrpc='2.0',id=2,method='tools/list'),
                  dict(jsonrpc='2.0',id=3,method='tools/call',params={'name':'search','arguments':{'query':'收到','account':'howiefire'}}),
                  dict(jsonrpc='2.0',id=4,method='tools/call',params={'name':'get_context','arguments':{'account':'howiefire','talker':'group@chatroom','server_id':101}})]
        p=subprocess.run([sys.executable,str(MODULE),'--root',str(self.store.root),'mcp'],
            input='\n'.join(json.dumps(r) for r in requests)+'\n',text=True,capture_output=True,check=True)
        replies=[json.loads(x) for x in p.stdout.splitlines()]
        self.assertEqual(len(replies),4)
        self.assertEqual(len(replies[1]['result']['tools']),2)
        self.assertEqual(json.loads(replies[2]['result']['content'][0]['text'])[0]['message']['server_id'],101)
        self.assertEqual(json.loads(replies[3]['result']['content'][0]['text'])[0]['message']['server_id'],101)
    def test_search_percent_is_literal(self):
        self.load(self.fixture([self.message(101,'100%'),self.message(102,'1000')]))
        self.assertEqual(len(self.store.search('%','howiefire')),1)

if __name__=='__main__': unittest.main()
