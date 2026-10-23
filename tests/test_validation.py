"""validation.py: checking requests and responses against every configured contract."""
import json

import httpx
import pytest

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


def test_issues_name_extra_and_missing_fields_without_values(tmp_path, make_client):
    c = make_client(schema(tmp_path, extra=False), replies=[(200, {'id': 1, 'amount': 150, 'secret': 'value'}), (200, {})],
               validate=True)
    extra = c.call('POST', '/things', body={'id': 1})
    issue = extra.validation['backend_api']['response']['errors'][0]['issues'][0]
    assert issue['rule'] == 'additionalProperties' and issue['extra'] == ['amount', 'secret']
    assert 'value' not in json.dumps(extra.validation)
    assert extra.view('validation').splitlines()[-1] == '  backend_api: response invalid: <root> extra amount, secret'
    missing = c.call('POST', '/things', body={'id': 1})
    assert missing.validation['backend_api']['response']['errors'][0]['issues'][0]['missing'] == ['id']
