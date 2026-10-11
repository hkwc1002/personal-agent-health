"""Linux transport acceptance with a disposable ledger and no remote calls."""
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'linux', 'SO_PEERCRED requires Linux')
class ServiceTest(unittest.TestCase):
    def run_service(self, allowed_uid):
        directory = tempfile.TemporaryDirectory(prefix='pah-')
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        env = {**os.environ, 'HEALTH_DATA': str(path / 'data'), 'HEALTH_SOCKET': str(path / 'h.sock'),
               'HEALTH_ALLOWED_UIDS': str(allowed_uid), 'HEALTH_INTERVALS_CONFIG': '',
               'HEALTH_TIMEZONE': 'UTC'}
        process = subprocess.Popen([sys.executable, '-B', str(ROOT / 'service' / 'server.py')],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop():
            process.terminate()
            process.communicate(timeout=10)
        self.addCleanup(stop)
        for _ in range(100):
            if (path / 'h.sock').exists():
                break
            if process.poll() is not None:
                self.fail('Service exited before readiness')
            time.sleep(.05)
        return path

    def request(self, path, operation, args, source='synthetic-test'):
        class Connection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(5)
                self.sock.connect(str(path / 'h.sock'))
        connection = Connection('localhost')
        try:
            connection.request('POST', '/call', json.dumps({'operation': operation, 'args': args, 'source': source}),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_socket_roundtrip_replay_and_timezone(self):
        path = self.run_service(os.getuid())
        status, result = self.request(path, 'status', {})
        self.assertEqual(status, 200)
        self.assertEqual(result['timezone'], 'UTC')
        args = {'items': [{'name': 'Synthetic meal', 'mass_g': {'low': 90, 'best': 100, 'high': 110}}]}
        self.assertTrue(self.request(path, 'record', args)[1]['ok'])
        self.assertTrue(self.request(path, 'record', args)[1]['duplicate'])
        self.assertEqual(self.request(path, 'today', {})[1]['meal_count'], 1)

    def test_socket_denies_unlisted_peer(self):
        path = self.run_service(os.getuid() + 100000)
        status, result = self.request(path, 'status', {})
        self.assertEqual(status, 403)
        self.assertFalse(result['ok'])
