"""Offline HTTP responses used only by test-update-install.sh."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import zipfile


class DownloadFixture(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.path == '/missing':
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', 'https://example.invalid/forbidden.zip')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path not in {'/archive', '/truncated', '/short', '/oversized'}:
            self.send_error(404)
            return
        body = self.server.payload
        if self.path == '/short':
            body = body[:1024]
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            if self.path == '/truncated':
                self.wfile.write(body[:1024])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
            else:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Oversized and truncated responses deliberately close early.
            pass

    def log_message(self, *_arguments):
        pass


def main():
    directory = Path(sys.argv[1])
    payload = bytes(range(256)) * 1024
    (directory / 'fixture-payload.bin').write_bytes(payload)
    with zipfile.ZipFile(directory / 'distribution.zip', 'w') as archive:
        archive.writestr('真实交互.app/Contents/Resources/中文 文件.txt', '合成应用内容')
        archive.writestr('__MACOSX/._真实交互.app', '合成元数据')
        alias = zipfile.ZipInfo('Applications')
        alias.create_system = 3
        alias.external_attr = 0o120777 << 16
        archive.writestr(alias, '/Applications')
        archive.writestr('安装与首次使用.md', '合成安装说明')
    with zipfile.ZipFile(directory / 'unsafe.zip', 'w') as archive:
        archive.writestr('真实交互.app/../../escape.txt', '不可解压')
    server = ThreadingHTTPServer(('127.0.0.1', 0), DownloadFixture)
    server.payload = payload
    ready = directory / 'download-server.json'
    temporary = ready.with_suffix('.tmp')
    temporary.write_text(json.dumps({'url': f'http://127.0.0.1:{server.server_port}'}))
    temporary.replace(ready)
    server.serve_forever()


if __name__ == '__main__':
    main()
