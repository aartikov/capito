"""HTTP transport on httpx and request encoding. Nothing is written to disk."""
from dataclasses import dataclass
import json
import socket
import sys
import threading
import time
import uuid
from urllib.parse import quote, urlencode

import httpcore
from httpcore._backends.sync import SyncStream  # not exported; the stream over an already connected socket
import httpx

from .files import File


@dataclass
class Request:
    """A ready-to-send request: the body is already encoded and Content-Type is in headers."""
    method: str
    url: str
    headers: dict
    body: bytes | None = None
    content_type: str | None = None
    path: dict | None = None              # the route's path values as text, before URL encoding; for validation


@dataclass
class Response:
    status: int                           # 0 when no response arrived
    headers: dict                         # lowercase names; a repeated header (Set-Cookie) is a list of values
    content: bytes
    duration: float = 0
    error: str | None = None


def first(value):
    """One value of a header that may repeat."""
    return value[0] if isinstance(value, list) else value


def interface_options(name):
    """Socket options that send connections through the named interface, whatever the routing table says."""
    if sys.platform == 'darwin':
        return [(socket.IPPROTO_IP, getattr(socket, 'IP_BOUND_IF', 25), socket.if_nametoindex(name))]
    if sys.platform.startswith('linux'):
        return [(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, name.encode())]
    raise OSError(f'interface is not supported on {sys.platform}')


class BoundBackend(httpcore.SyncBackend):
    """Connects through one interface. httpcore applies socket_options only after connecting, when the route
    is already chosen, so the options are set here before connect. The options are IPv4-level: IPv4 addresses only."""
    def __init__(self, options):
        self.options = options

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            sock = self._connect(host, port, timeout)
        except socket.timeout as exc:
            raise httpcore.ConnectTimeout(exc) from exc
        except OSError as exc:
            raise httpcore.ConnectError(exc) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return SyncStream(sock)

    def _connect(self, host, port, timeout):
        error = None
        for family, kind, proto, _, address in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
            sock = socket.socket(family, kind, proto)
            try:
                for option in self.options:
                    sock.setsockopt(*option)
                sock.settimeout(timeout)
                sock.connect(address)
                return sock
            except OSError as exc:
                sock.close()
                error = exc
        raise error


class HttpxTransport:
    """One connection pool per client; no proxies, certificates or .netrc from the environment, no redirects."""
    def __init__(self, config):
        self.config = config
        self._client = None
        self._guard = threading.Lock()

    def _http(self):
        with self._guard:
            if self._client is None:
                transport = httpx.HTTPTransport(retries=0, trust_env=False)
                if self.config.interface:
                    # httpx takes no network backend, so it goes into the pool httpx built.
                    transport._pool._network_backend = BoundBackend(interface_options(self.config.interface))
                self._client = httpx.Client(
                    transport=transport, trust_env=False,
                    follow_redirects=False, timeout=httpx.Timeout(self.config.timeout, connect=self.config.connect_timeout))
            return self._client

    def send(self, request):
        for key, value in request.headers.items():
            if any(c in str(key) + str(value) for c in "\r\n"):
                raise ValueError("Header injection is not allowed")
        start = time.monotonic()
        try:
            response = self._http().request(request.method, request.url, headers=request.headers, content=request.body)
        except (httpx.HTTPError, OSError) as exc:  # timeouts, refused connections, an unknown interface
            return Response(0, {}, b"", time.monotonic() - start, f"{type(exc).__name__}: {exc}")
        headers = {}
        for key, value in response.headers.multi_items():
            key = key.lower()
            if key in headers:
                previous = headers[key]
                headers[key] = (previous if isinstance(previous, list) else [previous]) + [value]
            else:
                headers[key] = value
        return Response(response.status_code, headers, response.content, time.monotonic() - start)

    def sent_headers(self, request):
        """The headers httpx sends with the request: its own (User-Agent, Accept…) unless the request sets them."""
        own = {name.lower() for name in request.headers}
        return {**{name: value for name, value in self._http().headers.items() if name.lower() not in own}, **request.headers}

    def close(self):
        with self._guard:
            if self._client is not None:
                self._client.close()
                self._client = None


def scalar(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def plain(kind, name, value):
    """A value that is one piece of text in the address: None and containers would go as Python text."""
    if value is None:
        hint = "pass a value" if kind == "Path" else "leave the key out to send none"
        raise TypeError(f"{kind} parameter {name!r} is None: {hint}")
    if isinstance(value, dict):
        raise TypeError(f"{kind} parameter {name!r}: encode objects explicitly, e.g. as separate keys")
    if isinstance(value, (list, tuple, set)):
        raise TypeError(f"{kind} parameter {name!r}: a {type(value).__name__} is not one value"
                        + ("; a list repeats a query key" if kind == "Query" else ""))
    return scalar(value)


def build_url(route, path, query):
    """Fill {name} templates and encode query; lists repeat the key, other shapes are explicit."""
    if not isinstance(route, str) or not route.startswith("/") or any(c in route for c in "?#"):
        raise ValueError(f"Route {route!r}: a path from / without ? or #, as in the contract; the query goes in query=")
    for name, value in path.items():
        route = route.replace("{" + name + "}", quote(plain("Path", name, value), safe=""))
    if "{" in route:
        raise ValueError("Path parameters missing")
    pairs = []
    for name, value in query.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        pairs.extend((name, plain("Query", name, v)) for v in values)
    return route + ("?" + urlencode(pairs) if pairs else "")


def multipart_part(boundary, name, value):
    if any(c in name for c in '\r\n"'):
        raise ValueError("Invalid multipart field name")
    if isinstance(value, File):
        if any(c in value.name + value.media_type for c in '\r\n"'):
            raise ValueError("Invalid multipart filename/content type")
        head = f'Content-Disposition: form-data; name="{name}"; filename="{value.name}"\r\nContent-Type: {value.media_type}'
        content = value.read()
    elif isinstance(value, (str, int, float, bool)):
        head = f'Content-Disposition: form-data; name="{name}"'
        content = scalar(value).encode()
    else:
        raise TypeError(f"Multipart field {name!r}: use text, numbers, bool, File or a list of them; encode nested data yourself")
    return f'--{boundary}\r\n{head}\r\n\r\n'.encode() + content + b"\r\n"


def media_type(content_type):
    """The type without parameters: 'application/json; charset=utf-8' -> 'application/json'."""
    return content_type.split(';', 1)[0].strip().lower()


def encode_body(*, body=None, multipart=None, raw=None, content_type=None):
    """Encode exactly one body kind; returns (bytes or None, media type or None).

    body follows the type: JSON for none, application/json and */*+json; a dict as a form for
    application/x-www-form-urlencoded; a str as UTF-8 text for any other type. Anything else is an error:
    encode it yourself and pass raw.
    """
    if multipart is not None:
        boundary = "capito-" + uuid.uuid4().hex
        parts = [multipart_part(boundary, name, item)
                 for name, value in multipart.items()
                 for item in (value if isinstance(value, (list, tuple)) else [value])]
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), "multipart/form-data; boundary=" + boundary
    if raw is not None:
        return raw, content_type or "application/octet-stream"
    if body is None:
        return None, content_type
    if isinstance(body, bytes):
        return body, content_type or "application/octet-stream"
    media = media_type(content_type) if content_type else "application/json"
    if media == "application/json" or media.endswith("+json"):
        return json.dumps(body, ensure_ascii=False, allow_nan=False).encode(), content_type or "application/json"
    if media == "application/x-www-form-urlencoded" and isinstance(body, dict):
        return urlencode(body, doseq=True).encode(), content_type
    if isinstance(body, str):
        return body.encode(), content_type
    raise TypeError(f"body for {content_type}: pass text as str, a dict for a form, "
                    "or encode it yourself and pass raw=; JSON types encode any body")
