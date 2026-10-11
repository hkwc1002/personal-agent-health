"""覆盖真实调度缺少user_task时的照片归属验证，不调用模型或正式账本。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class AdapterTest(unittest.TestCase):
    def setUp(self):
        test_root = Path(os.environ.get('HEALTH_TEST_ROOT',Path(__file__).resolve().parent/'_test-data'))
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.root = Path(self.temp.name)
        self.cache = self.root/'images';self.cache.mkdir()
        self.image = self.cache/'lunch.jpg';self.image.write_bytes(b'test image bytes')
        self.db = self.root/'state.db'
        with sqlite3.connect(self.db) as connection:
            connection.executescript('''CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT);
                CREATE TABLE messages(id INTEGER,session_id TEXT,platform_message_id TEXT,role TEXT,content TEXT);''')
            connection.execute('INSERT INTO sessions VALUES(?,?,?)',('s1','feishu','owner'))
            connection.execute('INSERT INTO messages VALUES(?,?,?,?,?)',
                (1,'s1','photo1','user','午餐\n[Image attached at: '+str(self.image)+']'))
            connection.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(2,'s1','other-message','user','文字消息'))
        connection.close()
        self.env = {'HERMES_SESSION_PLATFORM':'feishu','HERMES_SESSION_USER_ID':'owner',
                    'HERMES_SESSION_CHAT_TYPE':'dm',
                    'HERMES_SESSION_USER_ID_ALT':'owner','HERMES_SESSION_ID':'s1',
                    'HERMES_SESSION_MESSAGE_ID':'photo1'}
        context = types.ModuleType('gateway.session_context')
        context.get_session_env=lambda name,default='':self.env.get(name,default)
        registry = types.ModuleType('tools.registry')
        registry.registry=types.SimpleNamespace(register=lambda **kwargs:None)
        spec = importlib.util.spec_from_file_location('isolated_health_adapter',Path(__file__).resolve().parents[1]/'personal_health.py')
        self.adapter=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'gateway.session_context':context,'tools.registry':registry}):
            spec.loader.exec_module(self.adapter)
        self.requests=[]
        outer=self
        class Connection:
            def __init__(self,*args,**kwargs):pass
            def request(self,method,path,body,headers):outer.requests.append(json.loads(body))
            def getresponse(self):return types.SimpleNamespace(read=lambda limit:b'{"ok":true}')
            def close(self):pass
        self.stack=[patch.object(self.adapter,'Connection',Connection),
                    patch.object(self.adapter,'STATE_DB',self.db),
                    patch.object(self.adapter,'IMAGE_ROOTS',(self.cache,)),
                    patch.object(self.adapter,'OWNER_ID','owner')]
        if os.name=='nt':
            # Windows沙箱不开放严格路径解析；固定无软链接夹具，真实解析另在Linux验收。
            self.stack.append(patch.object(type(self.image),'resolve',lambda p,strict=False:p.absolute()))
        for p in self.stack:p.start()

    def tearDown(self):
        for p in reversed(self.stack):p.stop()
        self.temp.cleanup()

    def record(self,paths=None,**kwargs):
        return json.loads(self.adapter.health_record({'items':[{'name':'fixture'}],
            'image_paths':[str(self.image)] if paths is None else paths},**kwargs))

    def test_real_dispatch_without_user_task(self):
        result=self.record(task_id='actual-task',session_id='s1')
        self.assertTrue(result['ok'],result)
        request=self.requests[0]
        self.assertEqual(request['args']['image_hashes'],[hashlib.sha256(self.image.read_bytes()).hexdigest()])

    def test_readonly_lookup_does_not_modify_messages(self):
        self.record()
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0],2)
        connection.close()

    def test_other_message_cannot_borrow_photo(self):
        self.env['HERMES_SESSION_MESSAGE_ID']='other-message'
        self.assertFalse(self.record()['ok']);self.assertEqual(self.requests,[])

    def test_other_session_cannot_borrow_photo(self):
        self.env['HERMES_SESSION_ID']='s2'
        self.assertFalse(self.record()['ok']);self.assertEqual(self.requests,[])

    def test_owner_mismatch_rejected(self):
        self.env['HERMES_SESSION_USER_ID']='stranger';self.env['HERMES_SESSION_USER_ID_ALT']='stranger'
        self.assertFalse(self.record()['ok']);self.assertEqual(self.requests,[])

    def test_message_identity_required(self):
        self.env['HERMES_SESSION_MESSAGE_ID']=''
        result=self.record(task_id='actual-task')
        self.assertFalse(result['ok']);self.assertIn('消息标识',result['error']);self.assertEqual(self.requests,[])

    def test_outside_cache_rejected(self):
        other=self.root/'outside.jpg';other.write_bytes(b'outside')
        self.assertFalse(self.record(paths=[str(other)],user_task=str(other))['ok'])
        self.assertEqual(self.requests,[])

    def test_text_record_does_not_require_image_lookup(self):
        self.db.unlink()
        self.assertTrue(self.record(paths=[])['ok'])

    def test_owner_in_group_cannot_read_or_write(self):
        self.env['HERMES_SESSION_CHAT_TYPE'] = 'group'
        self.assertFalse(self.record()['ok'])
        self.assertFalse(json.loads(self.adapter.health_query({'action':'today'}))['ok'])
        self.assertEqual(self.requests, [])

    def test_missing_owner_fails_closed(self):
        with patch.object(self.adapter, 'OWNER_ID', ''):
            self.assertFalse(self.record()['ok'])
        self.assertEqual(self.requests, [])

    def test_background_write_is_rejected(self):
        self.env['HERMES_CRON_SESSION'] = '1'
        self.assertFalse(self.record()['ok'])
        self.assertEqual(self.requests, [])


if __name__=='__main__':unittest.main()
