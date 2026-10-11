"""仅 Unix socket 的健康服务，不监听公网端口，不接受任意命令或 SQL。"""
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import sys
from health import Health, dumps, object_keys
from intervals_sync import configured_sync

SERVICE = Health(os.environ.get('HEALTH_DATA', '/data'))
SOCKET = Path(os.environ.get('HEALTH_SOCKET', '/run/personal-agent-health/health.sock'))
ALLOWED_UIDS = {int(value) for value in os.environ.get('HEALTH_ALLOWED_UIDS', str(os.getuid())).split(',')}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, payload):
        raw = dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        pid, uid, gid = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid in ALLOWED_UIDS

    def do_GET(self):
        if self.path != '/health' or not self.allowed():
            return self.reply(404, {'ok': False})
        self.reply(200, {'ok': True, 'service': 'personal-agent-health'})

    def do_POST(self):
        if self.path != '/call' or not self.allowed():
            return self.reply(403, {'ok': False, 'error': '拒绝访问'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 1 <= length <= 262144:
                raise ValueError('请求大小不合法')
            request = json.loads(self.rfile.read(length))
            object_keys(request, {'operation', 'args', 'source'}, {'operation', 'args', 'source'})
            result = SERVICE.dispatch(request['operation'], request['args'], request['source'])
            if request['operation'] in ('record', 'amend', 'profile', 'targets', 'weight', 'workout', 'complete'):
                SERVICE.backup()
                # 只保留最近30次服务内快照；原库与异地备份各自管理。
                snapshots = sorted((SERVICE.root / 'backups').glob('health-*.sqlite3'))
                for old in snapshots[:-30]:
                    if old.is_file() and not old.is_symlink():
                        old.unlink()
            self.reply(200, {'ok': True, **result})
        except (ValueError, KeyError, TypeError) as exc:
            self.reply(400, {'ok': False, 'error': str(exc)[:400], 'do_not_claim_saved': True})
        except Exception:
            self.reply(503, {'ok': False, 'error': '健康服务失败；请查询实际记录，不要声称已保存', 'do_not_claim_saved': True})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


if __name__ == '__main__':
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET.exists():
        if not SOCKET.is_socket():
            raise SystemExit('拒绝覆盖非socket文件')
        SOCKET.unlink()
    with Server(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o660)
        print(json.dumps({'ready': True, 'unix_socket': str(SOCKET)}), flush=True)
        sync = configured_sync(SERVICE)
        if sync:
            sync.start()
        try:
            server.serve_forever()
        finally:
            if sync:
                sync.stop.set()
