"""Shared pieces of capito's own tests: a synthetic contract, client classes and respx answers."""
import json

import httpx

from capito.auth import BearerAuth
from capito.client import Client
from capito.accounts import AccountsCommands
from capito.config import view_limits
from capito.core import Core

BASE_URL = 'http://api.test'

# Typical limits of a project's capito.toml, for tests that check truncation; the library default is 'all'.
LIMITED_VIEW = view_limits({'depth': 1, 'items': 5, 'width': 80, 'lines': 60,
                            'help': {'width': 200, 'items': 'all', 'lines': 'all'}, 'list': {'depth': 0}})


def schema(tmp_path, name='api.json', *, extra=None):
    """A small OpenAPI contract: POST /things, GET /things and POST /refresh."""
    thing = {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'integer'}}}
    if extra is not None:
        thing['additionalProperties'] = extra
    ok = {'200': {'description': 'ok', 'content': {'application/json': {'schema': thing}}}}
    data = {'openapi': '3.1.0', 'info': {'title': 'test', 'version': '1'}, 'paths': {
        '/things': {'post': {'requestBody': {'required': True, 'content': {'application/json': {'schema': thing}}},
                             'responses': ok},
                    'get': {'responses': ok}},
        '/refresh': {'post': {'responses': {'200': {'description': 'tokens'}}}}}}
    path = tmp_path / name
    if path.suffix == '.json':
        path.write_text(json.dumps(data))
    else:
        import yaml
        path.write_text(yaml.safe_dump(data))
    return path


class TokenAuth(BearerAuth):
    """A minimal Bearer adapter: tokens in {'access', 'refresh'} of the response, refresh via POST /refresh."""
    access_token_path = 'access'
    refresh_token_path = 'refresh'

    def send_refresh(self, client, refresh_token, account, *, validate):
        return client.call('POST', '/refresh', body={'refresh': refresh_token}, validate=validate)


class AccountsClient(Client):
    """A client with the base accounts set and no authorization."""
    commands = {'accounts': AccountsCommands}


class TokenClient(Client):
    core = Core(auth=TokenAuth)
    commands = {'accounts': AccountsCommands}


def response(reply, request):
    """An httpx response for one queued reply: (status, json body or None), a callable of the request, or an exception."""
    if isinstance(reply, BaseException) or (isinstance(reply, type) and issubclass(reply, BaseException)):
        raise reply
    if callable(reply):
        return reply(request)
    status, body = reply
    headers = {'x-request-id': 'test-id'}
    return httpx.Response(status, headers=headers) if body is None else httpx.Response(status, json=body, headers=headers)


def answer(respx_mock, *replies):
    """One route for every request to BASE_URL, answering the replies in order; an extra request fails the test."""
    queue = list(replies)

    def respond(request):
        if not queue:
            raise AssertionError(f'Unexpected request: {request.method} {request.url}')
        return response(queue.pop(0), request)

    return respx_mock.route(host='api.test').mock(side_effect=respond)
