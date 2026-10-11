"""Read-only readiness probe used inside the health container."""
import http.client
import json
import os
import socket


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(os.environ.get('HEALTH_SOCKET', '/run/personal-agent-health/health.sock'))


if __name__ == '__main__':
    connection = Connection('localhost', timeout=3)
    try:
        connection.request('GET', '/health')
        response = connection.getresponse()
        result = json.loads(response.read(4096))
        if response.status != 200 or result.get('ok') is not True:
            raise SystemExit(1)
    finally:
        connection.close()
