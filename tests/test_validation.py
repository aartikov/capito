"""validation.py: checking requests and responses against every configured contract."""
import json
import threading
import time

import httpx
import pytest

from capito.files import File
from helpers import schema


@pytest.mark.parametrize('suffix', ['json', 'yaml', 'yml'])
def test_validation_informational_and_override(tmp_path, suffix, make_client):
    c = make_client(schema(tmp_path, 'api.' + suffix), replies=[(200, {'id': 'wrong'})] * 3)
    result = c.call('POST', '/things', body={'id': 'wrong'})
    assert result.success and result.validation is None
    result = c.call('POST', '/things', body={'id': 'wrong'}, validate=True)
    assert result.success
    assert result.validation['backend_api']['request']['status'] == 'invalid'
    assert result.validation['backend_api']['response']['status'] == 'invalid'
    json.loads(result.view(format='json'))
    c.core.validate = True
    assert c.call('POST', '/things', body={}, validate=False).validation is None


@pytest.mark.parametrize('extra,expected', [(None, 'valid'), (True, 'valid'), (False, 'invalid'), ({'type': 'integer'}, 'invalid')])
def test_additional_properties_library_rules(tmp_path, extra, expected, make_client):
    c = make_client(schema(tmp_path, extra=extra), replies=[(200, {'id': 1, 'extra': 'value'})], validate=True)
    result = c.call('POST', '/things', body={'id': 1})
    assert result.success
    assert result.validation['backend_api']['response']['status'] == expected


def test_two_contracts_optional_and_unavailable(tmp_path, make_client):
    backend = schema(tmp_path)
    analyst = schema(tmp_path, 'analyst.yaml', extra=False)
    c = make_client(backend, analyst, [(200, {'id': 1, 'extra': 1})], True)
    result = c.call('POST', '/things', body={'id': 1})
    assert result.validation['backend_api']['response']['status'] == 'valid'
    assert result.validation['analyst_api']['response']['status'] == 'invalid'
    c = make_client(backend, tmp_path / 'missing.yaml', [(200, {'id': 1})], True)
    result = c.call('POST', '/things', body={'id': 1})
    assert result.success and result.validation['analyst_api']['response']['status'] == 'unavailable'


def test_no_response_and_missing_operation(tmp_path, make_client):
    analyst = schema(tmp_path, 'analyst.json')
    data = json.loads(analyst.read_text())
    data['paths'] = {}
    analyst.write_text(json.dumps(data))
    c = make_client(schema(tmp_path), analyst, [httpx.ReadTimeout('timeout')], True)
    result = c.call('POST', '/things', body={'id': 1})
    assert result.validation['backend_api']['response']['status'] == 'not_run'
    assert result.validation['analyst_api']['request']['status'] == 'not_described'


def test_invalid_analyst_and_undescribed_body(tmp_path, make_client):
    backend = schema(tmp_path)
    analyst = tmp_path / 'bad.json'
    analyst.write_text('{bad')
    c = make_client(backend, analyst, [(200, {'id': 1})], True)
    assert c.call('POST', '/things', body={'id': 1}).validation['analyst_api']['request']['status'] == 'unavailable'
    data = json.loads(backend.read_text())
    data['paths']['/things']['post']['responses']['200']['content']['application/json'] = {}
    backend.write_text(json.dumps(data))
    c = make_client(backend, replies=[(200, {'anything': 1})], validate=True)
    assert c.call('POST', '/things', body={'id': 1}).validation['backend_api']['response']['status'] == 'not_described'


def test_validation_reuses_loaded_specs_and_does_not_change_transport_host(tmp_path, make_client, requests):
    backend = schema(tmp_path)
    data = json.loads(backend.read_text())
    data['servers'] = [{'url': 'https://schema.example.test/spec-prefix'}]
    backend.write_text(json.dumps(data))
    c = make_client(backend, replies=[(200, {'id': 1})] * 2, validate=True)
    first = c.call('POST', '/things', body={'id': 1})
    assert first.validation['backend_api']['request']['status'] == 'valid'
    assert first.validation['backend_api']['response']['status'] == 'valid'
    validator = c.core._validators['backend_api'].api
    second = c.call('POST', '/things', body={'id': 1})
    assert second.success and c.core._validators['backend_api'].api is validator
    assert all(str(r.url) == 'http://api.test/things' for r in requests())


def test_parallel_first_calls_wait_for_the_validator_instead_of_failing(make_client, monkeypatch):
    c = make_client(replies=[(200, {'id': 1})] * 8, validate=True)
    source, loads = c.core._sources['backend_api'], []
    read = source.load

    def slow_load():  # reading a large contract takes seconds; other requests arrive meanwhile
        loads.append(1)
        time.sleep(0.2)
        return read()

    monkeypatch.setattr(source, 'load', slow_load)
    start, results, failures = threading.Barrier(8), [], []

    def work():
        start.wait()
        try:
            results.append(c.call('GET', '/things'))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert [r.validation['backend_api']['response']['status'] for r in results] == ['valid'] * 8
    assert len(loads) == 1


def test_issues_name_extra_and_missing_fields_without_values(tmp_path, make_client):
    c = make_client(schema(tmp_path, extra=False), replies=[(200, {'id': 1, 'amount': 150, 'secret': 'value'}), (200, {})],
               validate=True)
    extra = c.call('POST', '/things', body={'id': 1})
    issue = extra.validation['backend_api']['response']['errors'][0]['issues'][0]
    assert issue['rule'] == 'additionalProperties' and issue['extra'] == ['amount', 'secret']
    assert 'value' not in json.dumps(extra.validation)
    assert extra.view('validation').splitlines()[-1] == "  backend_api: response invalid: <root> extra 'amount', 'secret'"
    missing = c.call('POST', '/things', body={'id': 1})
    assert missing.validation['backend_api']['response']['errors'][0]['issues'][0]['missing'] == ['id']


def files_contract(tmp_path, version='3.1.0'):
    """POST /files takes a form with a binary file; GET /files/{fileId} returns an image."""
    form = {'type': 'object', 'additionalProperties': False, 'required': ['file', 'purpose'],
            'properties': {'file': {'type': 'string', 'format': 'binary'}, 'purpose': {'type': 'string', 'enum': ['COVER']}}}
    data = {'openapi': version, 'info': {'title': 'test', 'version': '1'}, 'paths': {
        '/files': {'post': {'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': form}}},
                            'responses': {'201': {'description': 'saved'}}}},
        '/files/{fileId}': {'get': {
            'parameters': [{'name': 'fileId', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
            'responses': {'200': {'description': 'image',
                                  'content': {'image/png': {'schema': {'type': 'string', 'format': 'binary'}}}}}}}}}
    path = tmp_path / f'files-{version}.json'
    path.write_text(json.dumps(data))
    return path


def png(request):
    return httpx.Response(200, headers={'content-type': 'image/png'}, content=b'\x89PNG')


@pytest.mark.parametrize('version', ['3.0.3', '3.1.0'])
def test_binary_form_field_and_binary_response_are_valid(tmp_path, make_client, version):
    c = make_client(files_contract(tmp_path, version), replies=[(201, None), png], validate=True)
    upload = c.call('POST', '/files', multipart={'file': File(b'\x89PNG', filename='a.png', content_type='image/png'),
                                                 'purpose': 'COVER'})
    assert upload.validation['backend_api']['request']['status'] == 'valid'
    download = c.call('GET', '/files/{fileId}', path={'fileId': 'f-1'})
    assert download.validation['backend_api']['response']['status'] == 'valid'


def test_form_rules_other_than_the_file_type_still_apply(tmp_path, make_client):
    c = make_client(files_contract(tmp_path), replies=[(201, None)] * 2, validate=True)
    wrong = c.call('POST', '/files', multipart={'file': File(b'\x89PNG', filename='a.png'), 'purpose': 'OTHER'})
    missing = c.call('POST', '/files', multipart={'purpose': 'COVER'})
    assert [issue['rule'] for issue in wrong.validation['backend_api']['request']['errors'][0]['issues']] == ['enum']
    assert missing.validation['backend_api']['request']['errors'][0]['issues'][0]['missing'] == ['file']


def test_raw_bytes_are_a_string_only_with_the_binary_format():
    from capito.validation import OAS31BinaryValidator
    errors = lambda schema, instance: list(OAS31BinaryValidator(schema).iter_errors(instance))
    assert not errors({'type': 'string', 'format': 'binary'}, b'\x89PNG')
    assert not errors({'type': ['string', 'null'], 'format': 'binary'}, b'\x89PNG')
    assert errors({'type': 'string'}, b'\x89PNG')
    assert errors({'type': 'integer', 'format': 'binary'}, b'\x89PNG')
    assert errors({'type': 'string', 'format': 'binary'}, 1)


def items_contract(tmp_path):
    """GET /items needs a query limit; POST /items needs a bearer token and answers an item with three required fields."""
    item = {'type': 'object', 'required': ['name', 'status', 'owner'],
            'properties': {'name': {'type': 'string'}, 'status': {'type': 'string'}, 'owner': {'type': 'string'}}}
    ok = {'200': {'description': 'ok', 'content': {'application/json': {'schema': item}}}}
    data = {'openapi': '3.1.0', 'info': {'title': 'test', 'version': '1'},
            'components': {'securitySchemes': {'bearer': {'type': 'http', 'scheme': 'bearer'}}},
            'paths': {'/items': {
                'get': {'parameters': [{'name': 'limit', 'in': 'query', 'required': True,
                                        'schema': {'type': 'integer', 'maximum': 50}}], 'responses': ok},
                'post': {'security': [{'bearer': []}], 'responses': ok}}}}
    path = tmp_path / 'items.json'
    path.write_text(json.dumps(data))
    return path


def test_several_missing_fields_are_one_issue(tmp_path, make_client):
    c = make_client(items_contract(tmp_path), replies=[(200, {})], validate=True)
    result = c.call('GET', '/items', query={'limit': 10})
    assert result.validation['backend_api']['response']['errors'][0]['issues'] == [
        {'path': [], 'rule': 'required', 'expected': ['name', 'status', 'owner'], 'missing': ['name', 'status', 'owner']}]
    assert result.view('validation').splitlines()[-1] == "  backend_api: response invalid: <root> missing 'name', 'status', 'owner'"


@pytest.mark.parametrize('query,error,line', [
    ({'limit': 500}, {'in': 'query', 'name': 'limit'}, 'query.limit maximum'),
    (None, {'in': 'query'}, "query missing 'limit'"),
])
def test_parameter_errors_name_the_parameter(tmp_path, make_client, query, error, line):
    c = make_client(items_contract(tmp_path), replies=[(200, {'name': 'a', 'status': 'b', 'owner': 'c'})], validate=True)
    result = c.call('GET', '/items', query=query)
    assert error.items() <= result.validation['backend_api']['request']['errors'][0].items()
    assert result.view('validation').splitlines()[-1] == f'  backend_api: request invalid: {line}'


def test_missing_authorization_names_the_scheme(tmp_path, make_client):
    c = make_client(items_contract(tmp_path), replies=[(200, {'name': 'a', 'status': 'b', 'owner': 'c'})], validate=True)
    result = c.call('POST', '/items')
    assert result.validation['backend_api']['request']['errors'][0]['in'] == 'security'
    assert result.view('validation').splitlines()[-1] == "  backend_api: request invalid: security missing 'bearer'"


def test_the_reason_is_shown_for_a_status_without_violations(tmp_path, make_client):
    c = make_client(items_contract(tmp_path), tmp_path / 'missing.yaml', [(200, {'name': 'a', 'status': 'b', 'owner': 'c'})], True)
    line = c.call('GET', '/items', query={'limit': 10}).view('validation').splitlines()[-1]
    assert line.startswith('  analyst_api: unavailable (') and 'missing.yaml' in line


def test_an_undecodable_body_is_reported_without_its_content(make_client):
    broken_reply = lambda request: httpx.Response(200, headers={'Content-Type': 'application/json'},
                                                  content=b'{"access_token":"RESPONSE-SENTINEL",x')
    c = make_client(replies=[broken_reply], validate=True)
    result = c.call('POST', '/things', raw=b'{"password":"REQUEST-SENTINEL",broken', content_type='application/json')
    phases = result.validation['backend_api']
    assert phases['request']['status'] == phases['response']['status'] == 'invalid'
    # Only the validation report: the summary line and body of a broken response are the body as received.
    section = result.view('validation').split('\n', 1)[1]
    for shown in (json.dumps(result.validation), section, result.view('validation', format='json')):
        assert 'SENTINEL' not in shown
    assert 'not valid application/json' in phases['request']['errors'][0]['message'].lower()
    assert 'line 1 column' in phases['request']['errors'][0]['message']


def test_a_body_that_is_not_utf8_is_invalid_not_unavailable(make_client):
    c = make_client(replies=[(200, {'id': 1})], validate=True)
    request = c.call('POST', '/things', raw=b'\xff\xfe{"id": 1}', content_type='application/json').validation['backend_api']['request']
    assert request['status'] == 'invalid'


def test_a_cookie_value_with_characters_simplecookie_rejects_is_still_seen(tmp_path, make_client):
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['paths']['/things']['get']['parameters'] = [{'in': 'cookie', 'name': 'sid', 'required': True, 'schema': {'type': 'string'}}]
    path.write_text(json.dumps(data))
    c = make_client(path, replies=[(200, {'id': 1})] * 2, validate=True)
    seen = c.call('GET', '/things', headers={'Cookie': 'theme=dark; sid=<a value>'}).validation['backend_api']['request']
    missing = c.call('GET', '/things', headers={'Cookie': 'theme=dark'}).validation['backend_api']['request']
    assert seen['status'] == 'valid' and missing['status'] == 'invalid'


def test_bodies_of_any_json_type_are_checked_as_json(tmp_path, make_client):
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    thing = {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'integer'}}}
    data['paths']['/things']['post'] = {
        'requestBody': {'required': True, 'content': {'application/vnd.test+json': {'schema': thing}}},
        'responses': {'200': {'description': 'ok', 'content': {'application/problem+json': {'schema': thing}}}}}
    path.write_text(json.dumps(data))
    reply = lambda body: lambda request: httpx.Response(200, headers={'Content-Type': 'application/problem+json'}, json=body)
    c = make_client(path, replies=[reply({'id': 1}), reply({'id': 'x'})], validate=True)
    good = c.call('POST', '/things', body={'id': 1}, content_type='application/vnd.test+json').validation['backend_api']
    bad = c.call('POST', '/things', body={'id': 'x'}, content_type='application/vnd.test+json').validation['backend_api']
    assert good['request']['status'] == good['response']['status'] == 'valid'
    assert bad['request']['status'] == bad['response']['status'] == 'invalid'


@pytest.mark.parametrize('base_url', ['http://api.test/api/v1', 'http://api.test/api/v1/'])
def test_a_base_url_with_a_path_needs_no_servers_in_the_contract(make_client, base_url):
    c = make_client(replies=[(200, {'id': 1}), (200, {'id': 1})], validate=True, base_url=base_url.rstrip('/'))
    described = c.call('POST', '/things', body={'id': 1})
    assert described.validation['backend_api'] == {'request': {'status': 'valid', 'errors': []},
                                                   'response': {'status': 'valid', 'errors': []}}
    assert c.call('GET', '/others').validation['backend_api']['request']['status'] == 'not_described'


@pytest.mark.parametrize('name, schema, value, issue', [
    ('email', {'type': 'string', 'format': 'email'}, 'alice@example.test', None),
    ('email', {'type': 'string', 'format': 'email'}, 'not-an-email', 'path.email format'),
    ('name', {'type': 'string', 'maxLength': 4}, 'café', None),
    ('name', {'type': 'string', 'maxLength': 4}, 'cafés', 'path.name maxLength'),
    ('name', {'type': 'string', 'pattern': '^[a-z]+/[a-z]+$'}, 'a/b', None),
    ('name', {'type': 'integer', 'maximum': 10}, 7, None),
])
def test_a_path_value_is_checked_as_given_not_url_encoded(tmp_path, make_client, name, schema, value, issue):
    backend = tmp_path / 'paths.json'
    backend.write_text(json.dumps({'openapi': '3.0.3', 'info': {'title': 'test', 'version': '1'}, 'paths': {
        f'/users/{{{name}}}': {'get': {'parameters': [{'name': name, 'in': 'path', 'required': True, 'schema': schema}],
                                       'responses': {'200': {'description': 'ok'}}}}}}))
    c = make_client(backend, replies=[(200, None)], validate=True)
    result = c.call('GET', f'/users/{{{name}}}', path={name: value})
    expected = 'valid' if issue is None else f'request invalid: {issue}'
    assert result.view('validation').splitlines()[-1] == f'  backend_api: {expected}'


def test_headers_httpx_adds_are_seen_by_validation_as_sent(tmp_path, make_client, requests):
    backend = schema(tmp_path)
    data = json.loads(backend.read_text())
    data['components'] = {'securitySchemes': {'agent': {'type': 'apiKey', 'in': 'header', 'name': 'User-Agent'}}}
    data['paths']['/things']['get']['security'] = [{'agent': []}]
    data['paths']['/things']['get']['parameters'] = [{'name': 'Accept', 'in': 'header', 'required': True, 'schema': {'type': 'string'}}]
    backend.write_text(json.dumps(data))
    c = make_client(backend, replies=[(200, {'id': 1})] * 2, validate=True)
    default = c.call('GET', '/things')  # httpx sends User-Agent and Accept of its own
    assert default.validation['backend_api']['request'] == {'status': 'valid', 'errors': []}
    assert requests()[0].headers['user-agent'].startswith('python-httpx/')
    assert 'User-Agent' not in default.request['headers']  # the result shows what call() set, as documented
    own = c.call('GET', '/things', headers={'user-agent': 'robot/1'})
    assert own.validation['backend_api']['request']['status'] == 'valid' and requests()[1].headers['user-agent'] == 'robot/1'


@pytest.mark.parametrize('purpose, status, line', [
    ('X', 'valid', '  backend_api: valid'),
    (None, 'invalid', "  backend_api: request invalid: <root> missing 'purpose'"),
])
def test_a_form_boundary_keeps_its_case_in_validation(tmp_path, make_client, purpose, status, line):
    backend = schema(tmp_path)
    data = json.loads(backend.read_text())
    data['paths']['/upload'] = {'post': {'responses': {'200': {'description': 'ok'}}, 'requestBody': {'required': True, 'content': {
        'multipart/form-data': {'schema': {'type': 'object', 'required': ['purpose'], 'properties': {'purpose': {'type': 'string'}}}}}}}}
    backend.write_text(json.dumps(data))
    field = 'purpose' if purpose else 'other'
    form = f'--ABCdef\r\nContent-Disposition: form-data; name="{field}"\r\n\r\nX\r\n--ABCdef--\r\n'.encode()
    c = make_client(backend, replies=[(200, None)], validate=True)
    result = c.call('POST', '/upload', raw=form, content_type='Multipart/Form-Data; boundary=ABCdef')
    assert result.validation['backend_api']['request']['status'] == status
    assert result.view('validation').splitlines()[-1] == line
