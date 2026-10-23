"""The client. A project subclasses capito.Client and declares its parts as class attributes."""
from copy import deepcopy
from importlib import import_module
import json
import time

from .accounts import Session
from .auth import header
from .accounts import AccountsCommands
from .commands import Commands
from .config import Config
from .core import Core, CoreRuntime
from .files import File
from .results import error_from, strings, utcnow
from .transport import HttpxTransport, Request, build_url, encode_body, first


def check_arguments(as_user, session, body, multipart, raw, content_type):
    if session is not None and as_user is not None:
        raise ValueError('Choose as_user or a session snapshot, not both')
    if session is not None and not isinstance(session, Session):
        raise TypeError('session must be an accounts.session() snapshot')
    if sum(v is not None for v in (body, multipart, raw)) > 1:
        raise ValueError('Choose one of body, multipart or raw')
    if multipart is not None and content_type is not None:
        raise ValueError('content_type applies to body/raw; multipart sets its own type')
    if multipart is not None and not isinstance(multipart, dict):
        raise TypeError('multipart must be a dict of field names to values')
    if raw is not None and not isinstance(raw, bytes):
        raise TypeError('raw must be bytes')


def describe_multipart(multipart):
    """Export form fields without file contents."""
    if multipart is None:
        return None
    def item(value):
        return value.describe() if isinstance(value, File) else value
    return {k: [item(v) for v in value] if isinstance(value, (list, tuple)) else item(value)
            for k, value in multipart.items()}


def decode(response, result):
    """Put the parsed response body into result; a broken JSON body is an error, not an exception."""
    if not response.content:
        return
    media = (first(response.headers.get('content-type')) or '').lower()
    if 'json' in media:
        try:
            result.body = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            result.body = response.content.decode(errors='replace')
            result.errors.append('Invalid JSON response')
    elif media.startswith('text/'):
        result.body = response.content.decode(errors='replace')
    else:
        result.body = response.content


NO_ACCOUNTS = ("This client has no accounts: add commands['accounts'] = capito.AccountsCommands "
               "or a project subclass to use as_user")


def check_commands(cls):
    """The accounts set class of a client declaration, or None; declaration errors raise TypeError."""
    reserved = {name for name in dir(Client) if not name.startswith('_')}
    for name, set_class in cls.commands.items():
        if not isinstance(name, str) or not name.isidentifier() or name.startswith('_'):
            raise TypeError(f'Command set name must be an identifier, got {name!r}')
        if name in reserved:
            raise TypeError(f'Command set {name!r} would hide client.{name}; choose another name')
        if not (isinstance(set_class, type) and issubclass(set_class, Commands)):
            raise TypeError(f'commands[{name!r}] must be a subclass of capito.Commands')
        if (name == 'accounts') != issubclass(set_class, AccountsCommands):
            raise TypeError("The accounts set is commands['accounts'], a subclass of capito.AccountsCommands; "
                            f"{name!r} -> {set_class.__name__} breaks that")
    return cls.commands.get('accounts')


def client_class(config):
    """The class the configuration's `client` key leads to ('module:Class'); capito.Client without the key."""
    if config.client is None:
        return Client
    module, _, name = config.client.partition(':')
    try:
        target = import_module(module)
        for part in name.split('.'):
            target = getattr(target, part)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f'client = {config.client!r} cannot be imported: {exc}') from None
    if not (isinstance(target, type) and issubclass(target, Client)):
        raise ValueError(f'{config.client} is not a subclass of capito.Client')
    return target


class Client:
    """A client of an HTTP API. A project subclasses it (class Client(capito.Client)) and declares two things:

    core = Core(auth=..., secrets=..., error_parser=...)   capito's mechanisms; client.core runs them
    commands = {name: <Name>Commands, ...}                 command sets; client.<name> runs them

    The top level holds only the command sets and call(), operations(), describe(), close() and print(client).
    """
    core = Core()
    commands = {}

    def __init__(self, config, *, validate=False):
        """Reads no files and makes no requests: contracts load on first use."""
        declaration = type(self).core
        if not isinstance(declaration, Core):
            raise TypeError('Client.core must be capito.Core(...)')
        accounts = check_commands(type(self))
        self.core = CoreRuntime(self, declaration, config, HttpxTransport(config), validate, accounts)
        for name, cls in self.commands.items():
            instance = cls(self)
            setattr(self, name, instance)
            self.core.registry.register(name, instance)

    def __getattr__(self, name):
        if name == 'accounts':  # only reached when the client declares no accounts set
            raise AttributeError(NO_ACCOUNTS)
        raise AttributeError(f'{type(self).__name__!r} object has no attribute {name!r}')

    @classmethod
    def declared_name(cls):
        return f'{cls.__module__}:{cls.__qualname__}'

    @classmethod
    def from_config(cls, path=None, *, environment=None, **kwargs):
        """A client for capito.toml: the given path or the nearest one upwards from the working directory.

        The file's `client` key must lead to this very class (any import path to it will do), so code and the
        CLI use the same client. Without the key the file is for the base capito.Client.
        """
        config = Config.load(path, environment)
        if client_class(config) is not cls:
            if config.client is None:
                raise ValueError(f"{config.path.name} has no client key, so it is for capito.Client; "
                                 f"add client = '{cls.declared_name()}'")
            raise ValueError(f'{config.path} declares {config.client}, not {cls.declared_name()}')
        return cls(config, **kwargs)

    def close(self):
        """Close pooled connections; the client stays usable and reconnects on the next call."""
        close = getattr(self.core.transport, 'close', None)
        if close:
            close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def operations(self, text=None, *, kind=None):
        """Commands and contract operations; text filters id and summary, kind is 'command' or 'api'."""
        return self.core.registry.operations(text, kind=kind)

    def describe(self, identifier):
        """Help for command:<set>.<name>, api:<METHOD> <route> or schema:<Name>."""
        return self.core.registry.describe(identifier)

    def view(self):
        """Client, configuration, contracts and effective view limits; no credentials."""
        config = self.core.config
        lines = [f'{self.declared_name()} · {config.environment} → {config.base_url}']
        if config.path:
            lines.append(f'config: {config.path}')
        lines.append(f'state: {config.state}')
        lines.append('contracts:')
        for name, info in self.core.contracts().items():
            role = ' (help)' if name == config.help_contract else ''
            details = (f"sha256 {info['sha256'][:12]} · version {info.get('version')}" if 'sha256' in info
                       else 'unavailable' if 'unavailable' in info else 'not loaded')
            lines.append(f"  {name}{role}: {info['source']} · {details}")
        lines.append('view: ' + ' · '.join(f'{kind} {limits.text()}' for kind, limits in config.view.items()))
        if config.interface:
            lines.append(f'interface: {config.interface}')
        return '\n'.join(lines)

    def __repr__(self):
        return self.view()

    __str__ = __repr__

    def call(self, method, route, *, as_user=None, session=None, path=None, query=None,
             body=None, multipart=None, raw=None, content_type=None, headers=None,
             validate=None, auto_refresh=True):
        """Send exactly the described request; contracts are used only for optional validation.

        as_user/session attach credentials; nothing else is implicit. Local state changes only through the
        automatic refresh after 401 with as_user, which saves the new session.
        Session snapshots never refresh. No retries except one authorization refresh after 401.
        """
        started = time.monotonic()
        check_arguments(as_user, session, body, multipart, raw, content_type)
        method = method.upper()
        if as_user is not None and 'accounts' not in self.commands:
            raise ValueError(NO_ACCOUNTS)
        if as_user is not None and self.core.refreshing:
            raise RuntimeError('A refresh request is sent without as_user; it would trigger another refresh')
        account = session.record if session else self.accounts.resolve(as_user) if as_user is not None else None
        request = self._build_request(method, route, path, query, headers, body, multipart, raw, content_type)
        core = self.core
        supplied_auth = core.auth.has_credentials(request)
        if account and not supplied_auth:
            core.auth.prepare(request, account)
        result = core.result(method=method, route=route, started_at=utcnow(),
                             request={'method': method, 'url': request.url, 'headers': dict(request.headers),
                                 'body': '<raw body omitted>' if raw is not None else deepcopy(body),
                                 'multipart': describe_multipart(multipart)})
        result.add_secrets(*(strings(account.session) if account else ()),
                           header(request.headers, 'authorization'), header(request.headers, 'cookie'))
        enabled = core.validate if validate is None else validate
        can_refresh = auto_refresh and account is not None and session is None and not supplied_auth
        response, account = self._send(request, route, account, result, can_refresh=can_refresh, validate=enabled)
        result.status, result.headers = response.status, response.headers
        result.request_id = first(response.headers.get('x-request-id'))
        if response.error:
            result.errors.append(response.error)
        decode(response, result)
        if response.status not in range(200, 300):
            result.errors.append(f'Unexpected HTTP status: {response.status}')
        if enabled:
            result.validation = {name: {'request': validator.check(request, route, phase='request'),
                                           'response': validator.check(request, route, response, phase='response')}
                                    for name, validator in core._validators.items()}
        result.sent_account = account if account and session is None and not supplied_auth else None
        result.success = not result.errors
        if not result.success:
            code, message = core.error_parser(result.body) if response.status else (None, None)
            result.error = error_from(result.errors, code, message)
        result.duration, result.finished_at = time.monotonic() - started, utcnow()
        return result

    def _build_request(self, method, route, path, query, headers, body, multipart, raw, content_type):
        encoded, media = encode_body(body=body, multipart=multipart, raw=raw, content_type=content_type)
        headers = dict(headers or {})
        if media and header(headers, 'content-type') is None:
            headers['Content-Type'] = media
        url = self.core.config.base_url + build_url(route, path or {}, query or {})
        return Request(method, url, headers, encoded, header(headers, 'content-type'))

    def _send(self, request, route, account, result, *, can_refresh, validate):
        """One attempt, plus one retry after a successful refresh; returns the response and final account."""
        for attempt in range(2):
            response = self.core.transport.send(request)
            response.headers = {k.lower(): v for k, v in response.headers.items()}
            result.attempts.append({'status': response.status, 'duration': response.duration, 'error': response.error,
                                       'request_id': first(response.headers.get('x-request-id'))})
            if attempt == 1 or not can_refresh or response.error or not self.core.auth.should_refresh(request, response):
                return response, account
            refreshed = self.core.refresh_session(account, validate=validate)
            result.attempts.append({'action': 'refresh', 'result': refreshed})
            result.add_secrets(*refreshed.secret_values())
            if not refreshed.success:
                result.errors.append('Session refresh failed; explicit login may be needed')
                return response, account
            try:
                account = self.core._store.resolve(account.id)
            except ValueError:
                result.errors.append('Account removed after refresh')
                return response, account
            self.core.auth.prepare(request, account)
            result.add_secrets(*strings(account.session))
        return response, account
