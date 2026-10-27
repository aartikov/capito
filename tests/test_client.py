"""client.py: call(), declaration of command sets, from_config and print(client)."""
import json
import re

import httpx
import pytest

from capito.__main__ import load_client, main
from capito.client import Client
from helpers import schema


def test_mutating_timeout_is_not_retried_even_with_a_session(make_robot, requests):
    c = make_robot([httpx.ReadTimeout('timeout')])
    result = c.call('POST', '/things', body={'id': 1}, as_user='robot')
    assert not result.success and result.errors == ['ReadTimeout: timeout', 'Unexpected HTTP status: 0']
    assert len(requests()) == 1


@pytest.mark.parametrize('status', [0, 500, 502, 503])
def test_mutation_not_retried(status, make_client, requests):
    c = make_client(replies=[httpx.ReadTimeout('timeout') if status == 0 else (status, {})])
    result = c.call('POST', '/things', body={'id': 1})
    assert not result.success and len(requests()) == 1


def test_non_2xx_status_and_request_metadata(tmp_path, make_client):
    c = make_client(schema(tmp_path), replies=[(400, {'reason': 'invalid'})])
    result = c.call('POST', '/things', body={'id': 'invalid'}, headers={'Idempotency-Key': 'repeat-key'})
    assert not result.success and result.errors == ['Unexpected HTTP status: 400']
    assert result.body == {'reason': 'invalid'}
    assert result.status == 400 and result.request_id == 'test-id'
    assert result.request['headers']['Idempotency-Key'] == 'repeat-key' and result.method == 'POST' and result.route == '/things'
    assert result.duration >= 0 and result.started_at and result.finished_at
    assert result.attempts[0]['status'] == 400


def test_raw_call_adds_no_idempotency_key_even_if_contract_requires_it(tmp_path, make_client, requests):
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['paths']['/things']['post']['parameters'] = [{'in': 'header', 'name': 'Idempotency-Key', 'required': True, 'schema': {'type': 'string'}}]
    path.write_text(json.dumps(data))
    c = make_client(path, replies=[(200, {'id': 1})] * 2)
    c.call('POST', '/things', body={'id': 1})
    assert not any(k.lower() == 'idempotency-key' for k in requests()[0].headers)
    c.call('POST', '/things', headers={'idempotency-key': 'provided'})
    assert requests()[1].headers['idempotency-key'] == 'provided'


def test_route_absent_from_contract_is_sent_and_reported_undescribed(tmp_path, make_client, requests):
    c = make_client(schema(tmp_path), replies=[(200, {'ok': True})])
    result = c.call('patch', '/not/in/{kind}', path={'kind': 'contract'}, query={'tag': ['a', 'b']}, validate=True)
    assert result.success and result.method == 'PATCH' and result.route == '/not/in/{kind}'
    assert str(requests()[0].url).endswith('/not/in/contract?tag=a&tag=b')
    assert result.validation['backend_api']['request']['status'] == 'not_described'


@pytest.mark.parametrize('options,exception', [
    ({'raw': 'not-bytes'}, TypeError), ({'raw': b'{}', 'body': {}}, ValueError),
    ({'session': {}}, TypeError), ({'session': {}, 'as_user': 'a'}, ValueError),
])
def test_invalid_call_usage_never_sends_request(tmp_path, options, exception, make_client, requests):
    c = make_client(schema(tmp_path))
    with pytest.raises(exception):
        c.call('POST', '/things', **options)
    assert not requests()


@pytest.mark.parametrize('content_type,content,success,body', [
    ('application/json', b'{invalid', False, '{invalid'),
    ('text/plain', b'text', True, 'text'),
    ('application/json', b'', True, None),
    ('application/problem+json', b'{"title": "x"}', True, {'title': 'x'}),
    ('Application/JSON; charset=utf-8', b'[1]', True, [1]),
    ('application/x-ndjson', b'{"a": 1}\n{"a": 2}\n', True, '{"a": 1}\n{"a": 2}\n'),  # many documents: text
    ('application/jsonl', b'{"a": 1}\n', True, '{"a": 1}\n'),
    ('application/json-seq', b'\x1e{"a": 1}\n', True, '\x1e{"a": 1}\n'),
    ('application/octet-stream', b'\x00\x01', True, b'\x00\x01'),
    ('text/plain; charset=windows-1251', 'Привет'.encode('cp1251'), True, 'Привет'),
    ('text/html; Charset="KOI8-R"', 'Привет'.encode('koi8-r'), True, 'Привет'),
    ('application/json; charset=windows-1251', '{"name": "Привет"}'.encode('cp1251'), True, {'name': 'Привет'}),
    ('text/plain; charset=unknown-x', 'Привет'.encode(), True, 'Привет'),  # an unknown charset reads as UTF-8
    ('text/plain', 'Привет'.encode(), True, 'Привет'),
])
def test_response_decoding(tmp_path, content_type, content, success, body, make_client):
    c = make_client(schema(tmp_path), replies=[lambda r: httpx.Response(200, headers={'Content-Type': content_type}, content=content)])
    result = c.call('POST', '/things')
    assert result.success == success and result.body == body


def test_multipart_is_exclusive_and_exported_without_content(tmp_path, make_client, requests):
    from capito import File
    c = make_client(schema(tmp_path), replies=[(200, {'id': 1})])
    for conflict in ({'body': {'a': 1}}, {'raw': b'x'}, {'content_type': 'text/plain'}):
        with pytest.raises(ValueError):
            c.call('POST', '/things', multipart={'a': 'b'}, **conflict)
    result = c.call('POST', '/things', multipart={'purpose': 'X', 'file': File(b'secret-bytes', filename='a.png')})
    assert result.request['multipart'] == {'purpose': 'X', 'file': {'file': '<12 bytes>', 'filename': 'a.png', 'content_type': 'image/png'}}
    assert requests()[0].headers['Content-Type'].startswith('multipart/form-data; boundary=')
    assert 'secret-bytes' not in str(result)


def test_header_line_marks_missing_response(tmp_path, make_client):
    c = make_client(schema(tmp_path), replies=[httpx.ReadTimeout('timeout')])
    result = c.call('POST', '/things', body={'id': 1})
    assert result.status == 0 and str(result).startswith('— POST /things · ') and str(result).endswith(' · ReadTimeout: timeout')
    assert result.error == {'code': None, 'message': 'ReadTimeout: timeout'}


def test_raw_invalid_json_and_binary_memory_only(make_client):
    binary = lambda request: httpx.Response(200, headers={'content-type': 'application/octet-stream'}, content=b'abc')
    c = make_client(replies=[(200, {'id': 1}), binary], validate=True)
    result = c.call('POST', '/things', raw=b'{invalid', content_type='application/json')
    assert result.success and result.validation['backend_api']['request']['status'] == 'invalid'
    assert c.call('POST', '/things', body={'id': 1}, validate=False).body == b'abc'
    assert not c.core.config.state.exists()


def test_without_an_accounts_set_there_are_no_account_commands_and_no_as_user(make_client):
    c = make_client(replies=[(200, {'id': 1})])
    assert not [op for op in c.operations(kind='command')]
    with pytest.raises(AttributeError, match="commands\\['accounts'\\]"):
        c.accounts
    with pytest.raises(ValueError, match='This client has no accounts'):
        c.call('GET', '/things', as_user='alice')
    assert c.call('GET', '/things').success  # anonymous requests and help still work


@pytest.mark.parametrize('commands,message', [
    ({'accounts': 'not a class'}, 'subclass of capito.Commands'),
    ({'call': 'Things'}, 'would hide client.call'),
    ({'people': 'AccountsCommands'}, "accounts set is commands\\['accounts'\\]"),
    ({'accounts': 'Things'}, "accounts set is commands\\['accounts'\\]"),
    ({'bad-name': 'Things'}, 'identifier'),
])
def test_command_set_declaration_errors(commands, message, make_client):
    from capito.client import Client
    from capito.accounts import AccountsCommands
    from capito.commands import Commands

    class Things(Commands):
        pass

    classes = {'Things': Things, 'AccountsCommands': AccountsCommands}
    declared = {name: classes.get(value, value) for name, value in commands.items()}
    with pytest.raises(TypeError, match=message):
        make_client(client_class=type('C', (Client,), {'commands': declared}))


def test_from_config_requires_the_matching_class(project, monkeypatch):
    root, write = project
    monkeypatch.chdir(root)
    import tiny_api
    assert isinstance(tiny_api.Client.from_config(), tiny_api.Client)
    with pytest.raises(ValueError, match='declares tiny_api:Client, not capito.client:Client'):
        Client.from_config()
    write(None)
    with pytest.raises(ValueError, match='no client key, so it is for capito.Client'):
        tiny_api.Client.from_config()


@pytest.mark.parametrize('key', [None, 'capito:Client', 'capito.client:Client'])
def test_the_base_client_works_from_configuration_alone(project, monkeypatch, capsys, key):
    root, write = project
    write(key)
    monkeypatch.chdir(root)
    client = Client.from_config()  # any import path to the same class is the same client
    assert type(client) is Client and client.describe('api:GET /things').id == 'api:GET /things'
    assert main(['operations', '--kind', 'api']) == 0 and 'api:GET /things' in capsys.readouterr().out
    assert type(load_client()) is Client


def test_print_client_shows_configuration_contracts_and_limits(project, monkeypatch):
    root, _ = project
    monkeypatch.chdir(root)
    import tiny_api
    client = tiny_api.Client.from_config()
    before = str(client)
    assert before.splitlines()[0] == 'tiny_api:Client · dev → http://api.test'
    assert 'main (help): ' in before and 'not loaded' in before
    assert 'view: result depth=all items=all width=all lines=all' in before
    client.operations()
    assert 'sha256 ' in str(client) and 'version 1' in str(client)


@pytest.mark.parametrize('media', ['application/json; charset=utf-8', 'application/x-www-form-urlencoded'])
def test_the_type_in_content_type_or_in_headers_sends_the_same_request(make_client, requests, media):
    c = make_client(replies=[(200, {'id': 1})] * 2)
    c.call('POST', '/things', body={'id': 1}, content_type=media)
    c.call('POST', '/things', body={'id': 1}, headers={'content-type': media})
    first, second = requests()
    assert first.content == second.content and first.headers['content-type'] == second.headers['content-type'] == media
    assert first.content == (b'{"id": 1}' if 'json' in media else b'id=1')


@pytest.mark.parametrize('content_type, header', [
    ('text/plain', 'application/json'),
    ('text/plain', 'text/plain; charset=utf-8'),               # parameters are part of the type
    ('text/plain; charset=utf-8', 'text/plain; charset=UTF-8'),  # and keep their case
])
def test_content_type_and_a_different_content_type_header_are_an_error(make_client, content_type, header):
    c = make_client()
    with pytest.raises(ValueError, match='disagree'):
        c.call('POST', '/things', body='text', content_type=content_type, headers={'Content-Type': header})


@pytest.mark.parametrize('header', ['Text/Plain; charset=utf-8', 'text/plain;charset=utf-8', ' TEXT/PLAIN ; charset=utf-8'])
def test_one_type_in_content_type_and_the_header_is_one_request(make_client, requests, header):
    c = make_client(replies=[(200, {'id': 1})])
    c.call('POST', '/things', body='text', content_type='text/plain; charset=utf-8', headers={'Content-Type': header})
    assert requests()[0].headers['content-type'] == header and requests()[0].content == b'text'


def test_a_hidden_header_value_repeated_by_the_server_stays_hidden(make_client):
    echo = lambda request: httpx.Response(400, json={'error': f"key {request.headers['x-goog-api-key']} is not valid, "
                                                              f"tag {request.headers['x-tag']}"})
    c = make_client(replies=[echo])
    result = c.call('GET', '/things', headers={'x-goog-api-key': 'KEY-SENTINEL', 'x-tag': 'visible-tag'})
    shown = [str(result), result.view(format='json'), result.view('body', 'error')]
    assert not any('KEY-SENTINEL' in text for text in shown)
    assert all('visible-tag' in text for text in shown[1:])  # only headers the policy hides are hidden by value


def test_a_short_hidden_header_value_is_hidden_as_a_token_not_inside_other_text(make_client):
    echo = lambda request: httpx.Response(401, json={'error': f"Invalid key {request.headers['x-api-key']}", 'retry': 11})
    c = make_client(replies=[echo])
    result = c.call('GET', '/things', headers={'X-API-Key': '1'})  # a negative check with a bogus key
    assert re.match(r'401 GET /things · \d+\.\d\ds · Unexpected HTTP status: 401 · body \{error, retry\}$', str(result))
    assert json.loads(result.view('body', format='json'))['body'] == {'error': 'Invalid key <redacted>', 'retry': 11}
    assert json.loads(result.view('request', format='json'))['request']['headers'] == {'X-API-Key': '<redacted>'}


@pytest.mark.parametrize('name', ['Content-Type', 'content-type'])
def test_a_form_with_a_content_type_header_is_an_error_not_a_form_without_boundary(make_client, requests, name):
    c = make_client(replies=[(200, {'id': 1})])
    with pytest.raises(ValueError, match='multipart sets its own Content-Type'):
        c.call('POST', '/things', multipart={'purpose': 'x'}, headers={name: 'multipart/form-data'})
    assert requests() == []  # nothing was sent
    c.call('POST', '/things', multipart={'purpose': 'x'})
    assert re.fullmatch(r'multipart/form-data; boundary=\S+', requests()[0].headers['content-type'])


@pytest.mark.parametrize('path, query, message', [
    ({'id': None}, {}, "Path parameter 'id' is None: pass a value"),
    ({'id': ['a', 'b']}, {}, "Path parameter 'id': a list is not one value"),
    ({'id': {'a': 1}}, {}, "Path parameter 'id': encode objects explicitly"),
    ({'id': 'a'}, {'limit': None}, "Query parameter 'limit' is None: leave the key out"),
    ({'id': 'a'}, {'tag': ['a', None]}, "Query parameter 'tag' is None"),
    ({'id': 'a'}, {'f': [{'x': 1}]}, "Query parameter 'f': encode objects explicitly"),
    ({'id': 'a'}, {'f': [[1, 2]]}, "Query parameter 'f': a list is not one value; a list repeats a query key"),
])
def test_a_value_the_address_cannot_hold_is_an_error_not_python_text(make_client, requests, path, query, message):
    c = make_client()
    with pytest.raises(TypeError, match=re.escape(message)):
        c.call('GET', '/things/{id}', path=path, query=query)
    assert requests() == []


def test_scalars_and_lists_of_them_still_go_as_text(make_client, requests):
    c = make_client(replies=[(200, {'id': 1})])
    c.call('GET', '/things/{id}', path={'id': 7}, query={'on': True, 'share': 0.5, 'tag': ['a', 'b'], 'q': 'x y'})
    assert str(requests()[0].url) == 'http://api.test/things/7?on=true&share=0.5&tag=a&tag=b&q=x+y'


@pytest.mark.parametrize('route', ['/things?limit=1', 'things', '/things#top', ''])
def test_a_route_is_a_path_from_the_root_without_query(make_client, requests, route):
    c = make_client(base_url='http://api.test/api')
    with pytest.raises(ValueError, match='the query goes in query='):
        c.call('GET', route, query={'page': 2})
    assert requests() == []


def test_a_route_goes_after_the_path_of_base_url(make_client, requests):
    c = make_client(replies=[(200, {'id': 1})], base_url='http://api.test/api')
    c.call('GET', '/things', query={'page': 2})
    assert str(requests()[0].url) == 'http://api.test/api/things?page=2'


def test_header_values_go_as_text_like_query_values(make_client, requests):
    c = make_client(replies=[(200, {'id': 1})])
    result = c.call('GET', '/things', headers={'X-Page': 2, 'X-Dry-Run': True, 'X-Share': 0.5, 'X-Raw': b'r'})
    sent = requests()[0].headers
    assert (sent['x-page'], sent['x-dry-run'], sent['x-share'], sent['x-raw']) == ('2', 'true', '0.5', 'r')
    assert result.request['headers']['X-Page'] == '2'


@pytest.mark.parametrize('value, exception, message', [
    (None, TypeError, "Header parameter 'X-Name' is None"),
    (['a'], TypeError, "Header parameter 'X-Name': a list is not one value"),
    ('Алиса', ValueError, "Header parameter 'X-Name': a header value is ASCII text"),
])
def test_a_header_value_http_cannot_carry_is_an_error_naming_the_header(make_client, requests, value, exception, message):
    with pytest.raises(exception, match=re.escape(message)):
        make_client().call('GET', '/things', headers={'X-Name': value})
    assert requests() == []
