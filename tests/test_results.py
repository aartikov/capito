"""results.py: Secrets, Result, CompoundResult and Account."""
import hashlib
import json
import re

import pytest

from capito.client import Client
from capito.core import Core
from capito.results import Account, CompoundResult, Result, Secrets, add_step
from helpers import AccountsClient, LIMITED_VIEW, schema


def everything(result):
    """Every rendering of a result, without limits."""
    return '\n'.join([str(result), result.view(format='json'),
                      result.view('body', 'headers', 'request', 'attempts', 'errors', 'error',
                                  depth='all', items='all', width='all', lines='all')])


def test_report_keeps_http_contract_assertions_and_coverage_separate(tmp_path, make_client):
    c = make_client(replies=[(200, {'id': 1}), (200, {'id': 'bad'})], validate=True)
    results = [c.call('POST', '/things', body={'id': 1}), c.call('POST', '/things', body={'id': 'deliberately-invalid'})]
    report = {'contracts': c.core.contracts(), 'not_run': ['unrelated scenario'], 'cases': [
        {'expected_invalid_request': i == 1, 'http_ok': r.success, 'business_checks': {'has_id': 'id' in r.body},
         'result': json.loads(r.view(format='json'))}
        for i, r in enumerate(results)]}
    path = tmp_path / 'explicit-report.json'
    path.write_text(json.dumps(report))
    restored = json.loads(path.read_text())
    assert all(case['http_ok'] for case in restored['cases'])
    assert restored['cases'][1]['result']['validation']['backend_api'].startswith('request invalid')
    assert not c.core.config.state.exists()


def test_project_policy_hides_its_keys_without_affecting_other_clients(make_client):
    class SignedClient(Client):
        core = Core(secrets=Secrets.http().extend(keys={'Service-Signature'}))  # matches service_signature: case, - and _ are ignored
    reply = [(200, {'id': 1, 'service_signature': 'sig'})]
    signed = make_client(replies=reply, client_class=SignedClient).call('POST', '/things', body={'id': 1})
    plain = make_client(replies=reply).call('POST', '/things', body={'id': 1})
    assert json.loads(signed.view('body', format='json')) == {'body': {'id': 1, 'service_signature': '<redacted>'}}
    assert json.loads(plain.view('body', format='json')) == {'body': {'id': 1, 'service_signature': 'sig'}}
    assert signed.body['service_signature'] == 'sig'  # data for logic stays raw


def test_masking_covers_keys_values_and_known_values():
    result = Result(policy=Secrets.http(), body={'accessToken': 'a', 'refresh_token': 'b', 'password': 'p', 'name': 'visible',
                                         'note': 'sent Bearer abc.def', 'echo': 'opaque value inside'},
                    headers={'Authorization': 'Bearer a'}, errors=['opaque value'], secrets=['opaque value'])
    safe = json.loads(result.view(format='json'))
    assert safe['body'] == {'accessToken': '<redacted>', 'refresh_token': '<redacted>', 'password': '<redacted>',
                            'name': 'visible', 'note': 'sent Bearer <redacted>', 'echo': '<redacted> inside'}
    assert safe['headers']['Authorization'] == '<redacted>' and safe['errors'] == ['<redacted>']
    text = everything(result)
    assert 'opaque value' not in text and 'abc.def' not in text and "'a'" not in text
    assert result.body['accessToken'] == 'a'


def test_core_preset_hides_common_credentials_and_keeps_counters():
    jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl'
    result = Result(policy=Secrets.http(),
                    body={'accessToken': 'a', 'clientSecret': 's', 'sessionsCount': 3, 'tokensCount': 2,
                          'session': 'sess', 'csrf': 'c', 'error': f'expired token {jwt}', 'private_key': 'pk',
                          'tokens': {'access': 'a2'}, 'credentials': {'user': 'u'},
                          'usage': {'prompt_tokens': 12, 'total_tokens': 42, 'completion_tokens_details': {'reasoning_tokens': 5}},
                          'download': 'https://cdn.example.test/f.png?X-Amz-Signature=sig&api_key=k&size=2'},
                    request={'url': 'http://x/items?token=t1&page=2&q=Bearer leaked',
                             'headers': {'Cookie': 'c', 'X-API-Key': 'k', 'X-CSRF-Token': 'c'}})
    safe = json.loads(result.view(format='json'))
    assert safe['request'] == {'url': 'http://x/items?token=<redacted>&page=2&q=Bearer <redacted>',
                               'headers': {'Cookie': '<redacted>', 'X-API-Key': '<redacted>', 'X-CSRF-Token': '<redacted>'}}
    body = safe['body']
    assert body['accessToken'] == body['clientSecret'] == body['session'] == body['csrf'] == '<redacted>'
    assert body['private_key'] == body['tokens'] == body['credentials'] == '<redacted>'
    # token marks a key that ends with it; counters of tokens are not secrets
    assert body['sessionsCount'] == 3 and body['tokensCount'] == 2
    assert body['usage'] == {'prompt_tokens': 12, 'total_tokens': 42, 'completion_tokens_details': {'reasoning_tokens': 5}}
    assert body['error'] == 'expired token <redacted>'
    assert body['download'] == 'https://cdn.example.test/f.png?X-Amz-Signature=sig&api_key=<redacted>&size=2'



def test_hidden_keys_hide_their_values_in_json_text_that_did_not_parse():
    cut_off = ('{"access_token": "opaque-a1", "user": {"name": "alice", "password": 1234}, '
               '"tokens": {"refresh": "opaque-t1", "kind": ["x"]}, "note": "a \\"quoted\\" word", '
               '"client_secret": "opaque-\\"s1\\"", "expires_in": 3600, "refresh_token": "opaque-r1')
    result = Result(policy=Secrets.http(), success=False, status=200, body=cut_off, errors=['Invalid JSON response'])
    assert json.loads(result.view('body', format='json'))['body'] == (
        '{"access_token": <redacted>, "user": {"name": "alice", "password": <redacted>}, '
        '"tokens": <redacted>, "note": "a \\"quoted\\" word", '
        '"client_secret": <redacted>, "expires_in": 3600, "refresh_token": <redacted>')
    assert 'opaque' not in everything(result) and '1234' not in everything(result)


def test_api_keys_are_hidden_under_vendor_names_and_a_plain_key_only_when_declared():
    fields = dict(request={'url': 'http://x/items?key=k&page=2', 'headers': {'x-goog-api-key': 'g', 'X-RapidAPI-Key': 'r'}},
                  body={'googleApiKey': 'b'})
    preset = json.loads(Result(policy=Secrets.http(), **fields).view(format='json'))
    assert preset['request']['headers'] == {'x-goog-api-key': '<redacted>', 'X-RapidAPI-Key': '<redacted>'}
    assert preset['body'] == {'googleApiKey': '<redacted>'}
    assert preset['request']['url'] == 'http://x/items?key=k&page=2'  # `key` is an ordinary name in many APIs
    declared = Result(policy=Secrets.http().extend(keys=('key',)), **fields)
    assert json.loads(declared.view('request.url', format='json'))['request']['url'] == 'http://x/items?key=<redacted>&page=2'


def test_extended_policy_hides_signed_url_parameters_and_lists_its_rules():
    policy = Secrets.http().extend(keys=('x-amz-signature',))
    result = Result(policy=policy, body={'download': 'https://cdn.example.test/f.png?X-Amz-Signature=sig&size=2'})
    assert json.loads(result.view(format='json'))['body']['download'] == 'https://cdn.example.test/f.png?X-Amz-Signature=<redacted>&size=2'
    rules = str(policy)
    keys, values = rules.splitlines()
    assert keys.startswith('keys: ') and 'xamzsignature' in keys and '/token$/' in keys
    assert values.startswith('values: ') and 'bearer' in values


def test_key_rules_are_exact_keys_or_expressions_and_value_rules_are_expressions():
    policy = Secrets(keys=('pin.*', re.compile('^otp')))
    assert [policy.hides(k) for k in ('pin.*', 'pincode', 'otp_code', 'OTP-Attempt', 'hotpath')] == [True, False, True, True, False]
    with pytest.raises(TypeError, match='re.compile'):
        Secrets(values=('itm_secret',))
    with pytest.raises(TypeError, match='exact key'):
        Secrets(keys=(42,))


def test_results_are_not_dicts_and_require_a_policy():
    import pytest
    with pytest.raises(TypeError):
        Result(body={})
    result = Result(policy=Secrets.http(), body={'id': 1})
    assert not isinstance(result, dict)
    with pytest.raises(TypeError):
        json.dumps(result)  # no accidental raw export
    with pytest.raises(TypeError, match='Unknown Result fields'):
        Result(policy=Secrets.http(), bodyy={})


def test_header_line_names_status_route_error_validation_and_body_shape(make_client):
    c = make_client(replies=[(400, {'reason': 'invalid', 'a': 1, 'b': 2, 'c': 3})], validate=True)
    result = c.call('POST', '/things', body={'id': 'bad'})
    line = str(result)
    assert '\n' not in line
    assert line.startswith('400 POST /things · ')
    assert 'Unexpected HTTP status: 400' in line  # the core has no error format of its own
    assert 'backend_api: invalid' in line  # a body for an undeclared status is invalid too
    assert line.endswith('body {reason, a, b, +1}')
    assert result.error == {'code': None, 'message': 'Unexpected HTTP status: 400'}


def test_project_error_parser_fills_error(make_client):
    class Coded(Client):
        core = Core(error_parser=lambda body: (body.get('code'), body.get('text')))
    result = make_client(replies=[(403, {'code': 'Forbidden', 'text': 'No access'})], client_class=Coded).call('GET', '/things')
    assert result.error == {'code': 'Forbidden', 'message': 'No access'}
    assert 'Forbidden: No access' in str(result)
    ok = make_client(replies=[(200, {'id': 1})], client_class=Coded).call('GET', '/things')
    assert ok.error is None


@pytest.mark.parametrize('body, error, line', [
    ({'code': 404, 'text': 'Pet not found'}, {'code': 404, 'message': 'Pet not found'}, '404: Pet not found'),
    ({'code': 0}, {'code': 0, 'message': 'Unexpected HTTP status: 404'}, '0: Unexpected HTTP status: 404'),
])
def test_an_error_code_that_is_not_text_keeps_its_value_and_is_shown(make_client, body, error, line):
    class Numbered(Client):
        core = Core(error_parser=lambda body: (body.get('code'), body.get('text')))
    c = make_client(replies=[(404, body), (404, body)], client_class=Numbered)
    result = c.call('GET', '/things')
    assert result.error == error  # logic compares the API's own value: error['code'] == 404
    assert line in str(result)
    combined = c.core.compound('things.get')
    from capito.results import add_step
    add_step(combined, 'get', c.call('GET', '/things'))
    assert f'get: 404 {line}' in str(combined)


def test_compound_result_header_and_account_line(make_client):
    c = make_client(replies=[(201, {'id': 1}), (403, {'id': 2})], view=LIMITED_VIEW, client_class=AccountsClient)
    combined = c.core.compound('things.pair')
    from capito.results import add_step
    add_step(combined, 'first', c.call('POST', '/things', body={'id': 1}))
    combined.data['first_id'] = 1
    add_step(combined, 'second', c.call('POST', '/things', body={'id': 2}))
    assert str(combined) == 'FAILED things.pair · second: 403 Unexpected HTTP status: 403 · data: first_id=1'
    assert combined.view('steps').splitlines()[1:] == ['steps:', '  first: ' + str(combined.steps['first']),
                                                       '  second: ' + str(combined.steps['second'])]
    record = c.core._store.create('robot', {'kind': 'service'})
    account = c.core._store.public(record)
    assert isinstance(account, Account) and str(account) == 'robot  session=no'
    assert account == c.accounts.get('robot') and account.identity == {'kind': 'service'}


def test_compound_header_summarises_step_validation_per_contract():
    policy = Secrets.http()
    phases = lambda request, response: {'request': {'status': request}, 'response': {'status': response}}
    step = lambda status=200, **contracts: Result(policy=policy, success=status < 400, status=status, method='POST',
                                                  route='/things', validation=contracts or None)
    def compound(**steps):
        result = CompoundResult('things.make', policy=policy)
        for name, value in steps.items():
            add_step(result, name, value)
        return result
    same = dict(backend_api=phases('valid', 'valid'), analyst_api=phases('not_described', 'not_described'))
    assert str(compound(upload=step(**same), publish=step(**same))) == \
        'OK things.make · backend_api: valid · analyst_api: not_described'
    mixed = compound(upload=step(backend_api=phases('valid', 'invalid')), draft=step(backend_api=phases('valid', 'valid')),
                     publish=step(backend_api=phases('invalid', 'invalid')), cover=step(backend_api=phases('invalid', 'not_run')))
    assert str(mixed) == 'OK things.make · backend_api: upload resp=invalid, publish invalid, cover req=invalid resp=not_run'
    refused = compound(upload=step(backend_api=phases('valid', 'valid')), publish=step(409, backend_api=phases('valid', 'invalid')))
    assert str(refused) == 'FAILED things.make · publish: 409 failed · backend_api: publish resp=invalid'
    assert str(compound(local=step(), upload=step(backend_api=phases('valid', 'valid')))) == 'OK things.make · backend_api: valid'
    assert str(compound(upload=step(), publish=step())) == 'OK things.make'  # validation off: no summary
    nested = compound(login=compound(send_code=step(backend_api=phases('valid', 'invalid'))),
                      profile=step(backend_api=phases('valid', 'valid')))
    assert str(nested) == 'OK things.make · backend_api: login.send_code resp=invalid'


def test_step_validation_summary_reads_real_validation(make_client):
    c = make_client(replies=[(200, {'id': 1}), (200, {'id': 'not a number'})], validate=True)
    combined = c.core.compound('things.pair')
    add_step(combined, 'first', c.call('POST', '/things', body={'id': 1}))
    add_step(combined, 'second', c.call('POST', '/things', body={'id': 2}))
    assert str(combined) == 'OK things.pair · backend_api: second resp=invalid'
    assert 'response invalid' in combined.view('steps.second.validation')


def test_contract_provenance_and_missing_analyst(tmp_path, make_client):
    path = schema(tmp_path)
    c = make_client(path, tmp_path / 'absent.json', [(200, {'id': 1})], True)
    assert c.core.contracts()['backend_api'] == {'source': str(path), 'loaded': False}  # nothing read on creation
    assert c.call('POST', '/things', body={'id': 1}).success
    info = c.core.contracts()
    assert info['backend_api']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert info['backend_api']['version'] == '1'
    assert 'unavailable' in info['analyst_api']
    assert not c.core.config.state.exists()
