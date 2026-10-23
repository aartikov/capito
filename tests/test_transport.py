"""transport.py: request encoding, interface binding and the real httpx transport on loopback."""
import json
import sys
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from capito.files import File
from capito.testing import config
from capito.transport import HttpxTransport, Request, encode_body, interface_options
from helpers import schema


@pytest.mark.parametrize('platform,expected', [
    ('darwin', lambda s: [(s.IPPROTO_IP, 25, 7)]),
    ('linux', lambda s: [(s.SOL_SOCKET, getattr(s, 'SO_BINDTODEVICE', 25), b'en0')]),
])
def test_interface_binds_by_name_on_macos_and_linux(monkeypatch, platform, expected):
    import socket
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(socket, 'if_nametoindex', lambda name: 7, raising=False)
    if platform == 'linux' and not hasattr(socket, 'SO_BINDTODEVICE'):
        monkeypatch.setattr(socket, 'SO_BINDTODEVICE', 25, raising=False)
    assert interface_options('en0') == expected(socket)


def test_interface_is_a_configuration_error_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    with pytest.raises(ValueError, match='not supported on Windows'):
        config(tmp_path, schema(tmp_path), interface='en0')


@pytest.mark.respx(assert_all_called=False)  # on macOS the request never reaches the transport
def test_unknown_interface_is_a_failed_result_not_an_exception(loopback_config, server):
    # A live server, so only the interface can fail: macOS rejects the name before connecting, Linux when binding the socket.
    url, received = server
    response = HttpxTransport(replace(loopback_config, interface='nosuchif0')).send(Request('GET', url + '/x', {}))
    assert response.status == 0 and response.error and not received


@pytest.mark.parametrize('body,media,expected', [
    ({'id': 1}, None, b'{"id": 1}'),
    ({'id': [1, 2]}, 'application/x-www-form-urlencoded', b'id=1&id=2'),
    (b'bytes', None, b'bytes'),
    ('text', 'text/plain', b'text'),
    (None, None, None),
])
def test_body_encoding(body, media, expected):
    actual, _ = encode_body(body=body, content_type=media)
    assert actual == expected


@pytest.mark.parametrize('multipart', [
    {'bad\r\nname': 'x'}, {'bad"name': File(b'1', filename='a.bin')},
    {'file': File(b'1', filename='bad"name.png')}, {'file': File(b'1', filename='a.png', content_type='image/png\r\nX: y')},
])
def test_multipart_injection_rejected(multipart):
    with pytest.raises(ValueError, match='Invalid multipart'):
        encode_body(multipart=multipart)


def test_multipart_holds_text_and_file_parts_in_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'cover.png').write_bytes(b'\x89PNG')
    body, media = encode_body(multipart={
        'purpose': 'EVENT_COVER', 'public': True, 'tag': ['a', 'b'],
        'file': File('cover.png'),
        'fake': File(b'not an image', filename='photo.jpg', content_type='image/jpeg'),
    })
    boundary = media.removeprefix('multipart/form-data; boundary=')
    parts = body.split(b'--' + boundary.encode())[1:-1]
    assert [p.split(b'name="')[1].split(b'"')[0] for p in parts] == [b'purpose', b'public', b'tag', b'tag', b'file', b'fake']
    assert parts[1].endswith(b'\r\n\r\ntrue\r\n')
    assert b'filename="cover.png"\r\nContent-Type: image/png\r\n\r\n\x89PNG' in parts[4]
    assert b'filename="photo.jpg"\r\nContent-Type: image/jpeg\r\n\r\nnot an image' in parts[5]


def test_multipart_rejects_nested_values_and_nameless_bytes():
    with pytest.raises(TypeError, match='encode nested data yourself'):
        encode_body(multipart={'meta': {'a': 1}})
    with pytest.raises(ValueError, match='explicit filename'):
        File(b'data')


@pytest.fixture
def loopback_config(tmp_path, respx_mock):
    respx_mock.route(host='127.0.0.1').pass_through()  # the real loopback server below
    return config(tmp_path, schema(tmp_path), timeout=2, connect_timeout=1)


@pytest.fixture
def server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/slow':
                time.sleep(.3)
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/binary')
                self.end_headers()
                return
            status = 503 if self.path == '/unavailable' else 200
            self.send_response(status)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Set-Cookie', 'session=a')
            self.send_header('Set-Cookie', 'csrf=b')
            self.end_headers()
            try:
                self.wfile.write(b'\x00\xffbinary')
            except BrokenPipeError:
                pass

        def do_POST(self):
            received.append((self.path, dict(self.headers), self.rfile.read(int(self.headers.get('Content-Length', 0)))))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('X-Request-Id', 'local-request')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

    service = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{service.server_port}', received
    service.shutdown()
    service.server_close()
    thread.join()


def test_real_json_and_multipart_local_only(loopback_config, server, tmp_path):
    url, received = server
    transport = HttpxTransport(loopback_config)
    body, media = encode_body(body={'name': 'Майк'})  # non-ASCII on purpose: JSON keeps it as UTF-8
    result = transport.send(Request('POST', url + '/a?x=1', {'Authorization': 'Bearer test-secret', 'Content-Type': media}, body))
    assert result.status == 200 and not result.error
    assert result.headers['x-request-id'] == 'local-request'
    assert json.loads(received[0][2]) == {'name': 'Майк'}
    assert received[0][0] == '/a?x=1' and received[0][1]['Authorization'] == 'Bearer test-secret'
    image = tmp_path / 'image.png'
    image.write_bytes(b'\x89PNG\x00data')
    body, media = encode_body(multipart={'purpose': 'EVENT_COVER', 'file': File(image)})
    result = transport.send(Request('POST', url + '/upload', {'Content-Type': media}, body))
    assert result.status == 200
    assert b'name="purpose"' in received[1][2] and b'filename="image.png"' in received[1][2]
    assert b'\x89PNG\x00data' in received[1][2]
    assert received[1][1]['Content-Type'].startswith('multipart/form-data; boundary=')
    transport.close()


@pytest.mark.respx(assert_all_called=False)  # rejected before any request
def test_header_injection_rejected_before_sending(loopback_config):
    with pytest.raises(ValueError, match='Header injection'):
        HttpxTransport(loopback_config).send(Request('GET', 'http://127.0.0.1', {'X': 'a\r\nInjected: b'}))


@pytest.mark.parametrize('path,status', [('/binary', 200), ('/unavailable', 503)])
def test_real_binary_errors_and_repeated_headers(loopback_config, server, path, status):
    url, _ = server
    response = HttpxTransport(loopback_config).send(Request('GET', url + path, {}))
    assert response.status == status and not response.error
    assert response.content == b'\x00\xffbinary'
    assert response.headers['set-cookie'] == ['session=a', 'csrf=b']


def test_redirects_are_returned_not_followed(loopback_config, server):
    url, _ = server
    response = HttpxTransport(loopback_config).send(Request('GET', url + '/redirect', {}))
    assert response.status == 302 and response.headers['location'] == '/binary'


def test_real_timeout_is_returned(loopback_config, server):
    url, _ = server
    response = HttpxTransport(replace(loopback_config, timeout=.05)).send(Request('GET', url + '/slow', {}))
    assert response.error and response.status == 0


def test_proxy_variables_of_the_environment_are_ignored(loopback_config, server, monkeypatch):
    url, _ = server
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:9')
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    response = HttpxTransport(loopback_config).send(Request('GET', url + '/binary', {}))
    assert response.status == 200
