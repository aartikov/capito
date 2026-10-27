"""registry.py: @command declarations, operations(), describe() and help cards."""
import json
import re

import pytest

from capito.registry import body_param, command, query_param
from helpers import AccountsClient, LIMITED_VIEW, schema


def test_discovery_is_read_only_and_lists_contract_operations(make_client):
    c = make_client(client_class=AccountsClient)
    ids = [op.id for op in c.operations()]
    assert {'api:POST /things', 'api:GET /things', 'api:POST /refresh', 'command:accounts.list'} <= set(ids)
    assert len(ids) == len(set(ids))
    assert all(c.describe(i).id == i for i in ids)
    assert c.accounts.list() == []
    assert not c.core.config.state.exists()


def declared(**commands):
    """A client class with one command set 'items' holding the given methods."""
    from capito.client import Client
    from capito.commands import Commands
    return type('C', (Client,), {'commands': {'items': type('ItemsCommands', (Commands,), commands)}})


def test_command_declaration_errors_surface_when_the_client_is_created(make_client):
    from capito.registry import command

    @command('Typo in parameters', parameters={'item': {'description': 'the item'}})
    def typo(self, item_id, *, as_user=None, validate=None):
        pass

    @command('Undescribed argument')
    def bare(self, item_id, *, as_user=None, validate=None):
        pass

    @command('Compound without steps', result='compound')
    def compound(self, *, as_user=None, validate=None):
        pass

    for method, message in ((typo, 'describes no such arguments: item'), (bare, "describe argument 'item_id'"),
                            (compound, 'declares steps and data')):
        with pytest.raises(TypeError, match=message):
            make_client(client_class=declared(method=method))


def test_broken_contract_references_fail_help_but_not_calls(make_client):
    from capito.registry import command, path_param

    @command('Missing operation', api=('api:GET /missing',))
    def missing(self, *, as_user=None, validate=None):
        return self.client.call('GET', '/things')

    @command('Wrong path parameter', api=('api:GET /things',),
             parameters={'thing_id': path_param('api:GET /things', 'thingId', 'ID')})
    def wrong(self, thing_id, *, as_user=None, validate=None):
        pass

    c = make_client(replies=[(200, {'id': 1})], client_class=declared(missing=missing, wrong=wrong))
    with pytest.raises(ValueError) as error:
        c.operations()
    assert 'command:items.missing: api:GET /missing is not in the help contract' in str(error.value)
    assert "command:items.wrong: argument thing_id: api:GET /things has no path parameter 'thingId'" in str(error.value)
    with pytest.raises(ValueError, match='disagree with the help contract'):
        c.describe('api:GET /things')
    assert c.items.missing().success  # requests do not depend on help


def test_new_endpoint_and_command_register_without_catalog_edit(tmp_path, make_client):
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['paths']['/new'] = {'get': {'responses': {'204': {'description': 'no content'}}}}
    path.write_text(json.dumps(data))
    c = make_client(path, replies=[(204, None), (204, None)])
    assert c.describe('api:GET /new').commands is None
    assert c.call('GET', '/new').success

    class Feature:
        @command('New feature', api=('api:GET /new',), example='client.feature.read()')
        def read(self):
            return c.call('GET', '/new')

    c.core.registry.register('feature', Feature())
    assert c.describe('command:feature.read').summary == 'New feature'
    assert c.describe('api:GET /new').commands == 'command:feature.read'
    assert Feature().read().success
    with pytest.raises(ValueError):
        c.core.registry.register('feature', Feature())


class Items:
    def __init__(self, client):
        self.client = client

    @command('Create a thing', api=('api:POST /things',),
             parameters={'data': body_param('api:POST /things')})
    def create(self, data, *, as_user=None, validate=None):
        raise AssertionError('Discovery must never execute the method')

    @command('Explain a local transformation', result='none', effects='Does not call a server.',
             parameters={'value': {'type': 'str', 'description': 'Text supplied by the caller.', 'example': 'hello'}})
    def transform(self, value, /, *, validate=False):
        """This docstring is available to the reader without opening the source."""
        raise AssertionError('Discovery must never execute the method')


def contract_with_errors(tmp_path):
    """The synthetic contract plus a shared error object referenced by two error responses."""
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['components'] = {'schemas': {'Error': {'type': 'object', 'required': ['message'],
                                                'properties': {'message': {'type': 'string'}}}}}
    error = {'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Error'}}}}
    data['paths']['/things']['post']['responses'].update({'400': {'description': 'bad', **error},
                                                         '404': {'description': 'bad', **error}})
    path.write_text(json.dumps(data))
    return path


def test_command_help_is_flat_text_from_metadata_signature_and_contract(tmp_path, make_client):
    c = make_client(contract_with_errors(tmp_path))
    c.core.registry.register('items', Items(c))
    create = c.describe('command:items.create')
    assert all(isinstance(value, str) for value in create.fields.values())
    assert create.call == ('client.items.create(data, *, as_user=None, validate=None)\n'
                              '  data: dict — body of POST /things\n'
                              'data = {\n  id: integer\n}')
    assert create.returns == 'Result\n  body on 200 (ok): {\n    id: integer\n  }'
    assert create.example == "client.items.create(data={'id': 1})"
    assert create.effects is None and create.description is None
    transform = c.describe('command:items.transform')
    assert transform.example == "client.items.transform('hello')"
    assert transform.description.startswith('This docstring')
    assert transform.returns == 'None'
    assert not c.core.config.state.exists()


def test_operation_help_lists_every_response_and_defines_named_objects_once(tmp_path, make_client):
    c = make_client(contract_with_errors(tmp_path))
    c.core.registry.register('items', Items(c))
    help_ = c.describe('api:POST /things')
    assert help_.auth == 'none' and help_.commands == 'command:items.create'
    assert help_.request == 'body [application/json]: {\n  id: integer\n}'
    assert help_.returns == ('Result\n'
                                '  body on 200 (ok): {\n    id: integer\n  }\n'
                                '  body on 400, 404 (bad): Error\n'
                                'Error = {\n  message: string\n}')
    assert help_.example == "client.call('POST', '/things', body={'id': 1})"


def test_help_tells_a_response_without_body_from_an_undescribed_one(tmp_path, make_client):
    data = json.loads(schema(tmp_path).read_text())
    data['paths']['/things']['delete'] = {'responses': {
        '200': {'description': 'gone'},
        '202': {'description': 'queued', 'content': {'application/json': {}}},
        '203': {'description': 'any', 'content': {'application/json': {'schema': {}}}},
        '204': {'description': 'empty'}, '304': {'description': 'same'}}}
    data['paths']['/things']['head'] = {'responses': {'200': {'description': 'exists'}}}
    path = tmp_path / 'bodies.json'
    path.write_text(json.dumps(data))
    c = make_client(path)
    assert c.describe('api:DELETE /things').returns == ('Result\n'
                                                       '  body on 200 (gone): not described\n'
                                                       '  body on 202 (queued): not described [application/json]\n'
                                                       '  body on 203 (any): any\n'
                                                       '  body on 204 (empty): none\n'
                                                       '  body on 304 (same): none')
    assert c.describe('api:HEAD /things').returns == 'Result\n  body on 200 (exists): none'


def test_local_commands_name_their_result_type(make_client):
    c = make_client(client_class=AccountsClient)
    kinds = {name: c.describe(f'command:accounts.{name}').returns for name in ('list', 'get', 'session', 'forget')}
    assert kinds == {'list': 'list[Account]', 'get': 'Account', 'session': 'Session', 'forget': 'None'}


def test_help_never_reads_saved_credentials(monkeypatch, make_robot):
    c = make_robot(access='secret-access', refresh='secret-refresh')
    def forbidden(*args, **kwargs):
        raise AssertionError('Help accessed accounts or network')
    monkeypatch.setattr(c.core._store, 'records', forbidden)
    monkeypatch.setattr(c.core.transport, 'send', forbidden)
    rendered = '\n'.join(c.describe(op.id).view(depth='all') for op in c.operations())
    assert 'secret-access' not in rendered and 'secret-refresh' not in rendered


def test_commands_must_declare_their_arguments(make_client):
    c = make_client()

    class Loose:
        @command('Pass anything through', api=('api:GET /things',))
        def read(self, **options):
            pass

    with pytest.raises(ValueError, match='declare arguments explicitly'):
        c.core.registry.register('loose', Loose())


def test_query_help_shows_fields_and_optional_query_stays_out_of_example(tmp_path, make_client):
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['paths']['/things']['get']['parameters'] = [
        {'name': 'page', 'in': 'query', 'schema': {'type': 'integer', 'default': 1}},
        {'name': 'q', 'in': 'query', 'schema': {'type': 'string'}}]
    path.write_text(json.dumps(data))
    c = make_client(path)

    class Things:
        @command('List things', api=('api:GET /things',), parameters={'query': query_param('api:GET /things')})
        def list(self, *, query=None, as_user=None, validate=None):
            pass

    c.core.registry.register('things', Things())
    listing = c.describe('command:things.list')
    assert listing.call.endswith('query = {\n  page?: integer(default=1)\n  q?: string\n}')
    assert listing.example == 'client.things.list()'


def test_a_command_query_example_spreads_an_object_parameter_like_call_needs(tmp_path, make_client, respx_mock):
    c = make_client(varied_contract(tmp_path), validate=True)
    respx_mock.route(host='api.test').respond(200)

    class Things:
        @command('Filter things', api=('api:GET /filtered',), parameters={'query': query_param('api:GET /filtered')})
        def filtered(self, query, *, as_user=None, validate=None):
            return c.call('GET', '/filtered', query=query, validate=validate)

    c.core.registry.register('things', Things())
    example = c.describe('command:things.filtered').example
    assert example == "client.things.filtered(query={'filter[status]': 'example', 'filter[limit]': 2, 'point': 'x,1,y,true'})"
    assert eval(example, {'client': type('C', (), {'things': Things()})}).validation['backend_api']['request']['status'] == 'valid'


def nested_contract(tmp_path):
    """Things reference Owner, Owner references Avatar; Empty is closed and has no fields."""
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    ref = lambda name: {'$ref': f'#/components/schemas/{name}'}
    data['components'] = {'schemas': {
        'Thing': {'type': 'object', 'properties': {'owner': ref('Owner'), 'kind': {'enum': list('ABCDEFG')}}},
        'Owner': {'type': 'object', 'description': 'Who owns it', 'properties': {'avatar': ref('Avatar')}},
        'Avatar': {'type': 'object', 'properties': {'url': {'type': 'string'}}},
        'Empty': {'type': 'object', 'additionalProperties': False}}}
    data['paths']['/things']['get']['responses']['200']['content']['application/json']['schema'] = ref('Thing')
    path.write_text(json.dumps(data))
    return path


def test_help_depth_defines_named_schemas_level_by_level(tmp_path, make_client):
    unlimited = make_client(nested_contract(tmp_path)).describe('api:GET /things')
    assert 'Avatar = {' in unlimited.returns  # without [view] limits the card is complete
    c = make_client(nested_contract(tmp_path), view=LIMITED_VIEW)
    card = c.describe('api:GET /things')
    assert 'Thing = {' in card.returns and 'Owner = {' not in card.returns  # depth 1 from [view.help]
    assert 'Owner = {' in card.view(depth=2) and 'Avatar = {' not in card.view(depth=2)
    assert 'Avatar = {' in card.view(depth='all')
    assert str(card) == card.view() and card.view().startswith('api:GET /things — ')
    assert 'kind?: A|B|C|…+4 (items=3)' in card.view(items=3) and 'A|B|C|D|E|F|G' in card.view()
    assert card.view('returns', 'nothing').splitlines()[-1] == 'nothing: <missing>'
    assert json.loads(card.view('example', format='json')) == {'example': card.example}


@pytest.mark.parametrize('identifier', ['api:GET /things', 'schema:Thing'])
def test_json_help_is_the_complete_card_whatever_the_limits(tmp_path, make_client, identifier):
    complete = json.loads(make_client(nested_contract(tmp_path)).describe(identifier).view(format='json'))
    assert 'Avatar = {' in json.dumps(complete) and 'A|B|C|D|E|F|G' in json.dumps(complete)
    card = make_client(nested_contract(tmp_path), view=LIMITED_VIEW).describe(identifier)  # depth 1 from [view.help]
    for limits in ({}, {'depth': 0}, {'depth': 1, 'items': 2}):
        assert json.loads(card.view(format='json', **limits)) == complete
    assert 'Avatar = {' not in card.view(depth=1)  # text keeps its limits


def test_inline_objects_take_levels_like_named_ones(tmp_path, make_client):
    c = make_client(nested_contract(tmp_path))
    obj = lambda **fields: {'type': 'object', 'properties': fields}
    text = {'type': 'string'}
    inline = obj(id=text, owner=obj(name=text, avatar=obj(url=text)),
                 tags={'type': 'array', 'items': obj(a=text, b=text, c=text, d=text)},
                 rows={'type': 'array', 'items': obj(owner=obj(avatar=obj(url=text)))})
    notation = c.core.contract.schema_text
    assert notation(inline, depth=0) == '{id, owner, tags, +1} ↓4'  # root → rows → owner → avatar
    assert notation(inline, depth=1) == ('{\n  id?: string\n  owner?: {name, avatar} ↓2\n'
                                         '  tags?: {a, b, c, +1}[]\n  rows?: {owner}[] ↓3\n}')  # one level of values: no marker
    assert '    avatar?: {url}\n' in notation(inline, depth=2) and '      url?: string' in notation(inline, depth=None)
    # allOf merges into the same object, so an alias defines its target at the alias's level
    assert notation({'allOf': [{'$ref': '#/components/schemas/Thing'}]}, depth=1).startswith('Thing\nThing = {\n  owner?: Owner ↓2\n')


def mixed_contract(tmp_path):
    """Named and inline objects interleaved, a schema reused at different levels, a cycle, two response bodies."""
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    ref = lambda name: {'$ref': f'#/components/schemas/{name}'}
    obj = lambda **fields: {'type': 'object', 'required': list(fields), 'properties': fields}
    text = {'type': 'string'}
    data['components'] = {'schemas': {
        'Root': obj(id=text, a=obj(b=ref('Mid'))),  # levels: Root 0, a 1, Mid 2, c 3, Leaf 4
        'Mid': obj(name=text, c=obj(leaf=ref('Leaf'))),
        'Leaf': obj(x=text, y=text),
        'Order': obj(deep=obj(inner=ref('Mid')), shallow=ref('Mid')),  # Mid is met at level 2 first, then 1
        'Node': obj(name=text, children={'type': 'array', 'items': ref('Node')}),
    }}
    responses = data['paths']['/things']['get']['responses']
    responses['200']['content']['application/json']['schema'] = obj(wrap=obj(item=ref('Mid')))
    responses['404'] = {'description': 'gone', 'content': {'application/json': {'schema': ref('Mid')}}}
    path.write_text(json.dumps(data))
    return path


def test_depth_sums_named_and_inline_levels_and_markers_say_what_is_missing(tmp_path, make_client):
    c = make_client(mixed_contract(tmp_path))
    root = {'$ref': '#/components/schemas/Root'}
    notation = lambda depth: c.core.contract.schema_text(root, depth=depth)
    assert c.core.contract.levels(root) == 5
    assert notation(0) == 'Root ↓5'
    assert '  a: {b} ↓4' in notation(1)
    assert '    b: Mid ↓3' in notation(2) and 'Mid =' not in notation(2)
    assert '  c: {leaf} ↓2' in notation(3)
    assert '    leaf: Leaf\n' in notation(4) and 'Leaf =' not in notation(4)  # only values inside: +1, no marker
    for depth in range(5):  # the largest marker is exactly the depth still missing
        assert max(map(int, re.findall(r'↓(\d+)', notation(depth))), default=1) == 5 - depth
    assert notation(5) == notation(None) and '↓' not in notation(5)


def test_a_reused_schema_is_defined_from_its_shallowest_occurrence(tmp_path, make_client):
    c = make_client(mixed_contract(tmp_path))
    order = lambda depth: c.core.contract.schema_text({'$ref': '#/components/schemas/Order'}, depth=depth)
    assert '    inner: Mid\n' in order(2) and '  c: {leaf} ↓2' in order(2)  # defined via shallow: no marker on inner
    assert '  c: {\n    leaf: Leaf\n  }' in order(3) and 'Leaf = {' in order(4)
    # one card shares definitions: 200 reaches Mid at level 2, 404 at its root
    returns = c.describe('api:GET /things').view('returns', depth=2)
    assert '    item: Mid\n' in returns and '\nMid = {\n  name: string\n  c: {\n    leaf: Leaf\n  }\n}' in returns


def test_a_recursive_schema_terminates_and_counts_its_cycle_once(tmp_path, make_client):
    c = make_client(mixed_contract(tmp_path))
    node = {'$ref': '#/components/schemas/Node'}
    full = 'Node\nNode = {\n  name: string\n  children: Node[]\n}'
    assert c.core.contract.schema_text(node, depth=None) == c.core.contract.schema_text(node, depth=9) == full
    assert c.core.contract.schema_text(node, depth=0) == 'Node ↓2'


def test_schema_help_closed_objects_and_suggestions(tmp_path, make_client):
    c = make_client(nested_contract(tmp_path), client_class=AccountsClient)
    owner = c.describe('schema:Owner')
    assert owner.summary == 'Who owns it' and owner.schema.startswith('Owner = {\n  avatar?: Avatar\n}')
    assert str(owner).splitlines()[:2] == ['schema:Owner — Who owns it', 'schema: Owner = {']
    assert c.describe('schema:Owner').view(depth=0) == 'schema:Owner — Who owns it\nschema: Owner ↓2'  # level 0 collapsed
    empty = c.describe('schema:Empty')  # no description: the header is the id alone
    assert str(empty) == 'schema:Empty\nschema: object(closed)' and empty.summary is None
    with pytest.raises(ValueError, match='did you mean Owner'):
        c.describe('schema:owner')
    with pytest.raises(ValueError, match='did you mean api:GET /things'):
        c.describe('api:GET /thing')
    with pytest.raises(ValueError, match='did you mean command:accounts.list'):
        c.describe('command:account.list')
    with pytest.raises(ValueError, match='schema:<Name>'):
        c.describe('Owner')


def test_operations_filter_by_text_and_kind_and_point_to_wrappers(tmp_path, make_client):
    c = make_client(contract_with_errors(tmp_path), client_class=AccountsClient)
    c.core.registry.register('items', Items(c))
    assert [o.id for o in c.operations('THING', kind='api')] == ['api:GET /things', 'api:POST /things']
    assert [o.id for o in c.operations('create', kind='command')] == ['command:accounts.create', 'command:items.create']
    rows = str(c.operations('things', kind='api')).splitlines()
    assert rows[1].startswith('api:POST /things  ') and rows[1].endswith('  → command:items.create')
    assert '→' not in rows[0]
    detailed = c.operations('transform').view(depth=1).splitlines()
    assert detailed == ['command:items.transform  Explain a local transformation',
                        "    client.items.transform(value, /, *, validate=False)", '    effects: Does not call a server.']
    with pytest.raises(ValueError, match="kind is 'command', 'api' or None"):
        c.operations(kind='schema')


def secured_contract(tmp_path):
    """One GET operation per way to authorize, plus one that also allows anonymous calls."""
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['components'] = {'securitySchemes': {
        'bearer': {'type': 'http', 'scheme': 'bearer'}, 'basic': {'type': 'http', 'scheme': 'basic'},
        'oauth': {'type': 'oauth2', 'flows': {}},
        'headerKey': {'type': 'apiKey', 'in': 'header', 'name': 'x-goog-api-key'},
        'queryKey': {'type': 'apiKey', 'in': 'query', 'name': 'key'},
        'cookieKey': {'type': 'apiKey', 'in': 'cookie', 'name': 'sid'}}}
    ok = {'200': {'description': 'ok'}}
    data['paths'] = {f'/{route}': {'get': {'security': security, 'responses': ok}} for route, security in {
        'bearer': [{'bearer': []}], 'basic': [{'basic': []}], 'oauth': [{'oauth': []}],
        'header': [{'headerKey': []}, {'queryKey': []}], 'query': [{'queryKey': []}],
        'cookie': [{'cookieKey': []}], 'open': [{'bearer': []}, {}]}.items()}
    path.write_text(json.dumps(data))
    return path


@pytest.mark.filterwarnings('ignore:Unsupported scheme type')  # openapi-core does not check oauth2
def test_without_accounts_examples_spell_out_the_credentials_the_contract_asks_for(tmp_path, make_client, respx_mock):
    c = make_client(secured_contract(tmp_path), validate=True)
    examples = {op.id: c.describe(op.id).example for op in c.operations(kind='api')}
    assert examples == {
        'api:GET /basic': "client.call('GET', '/basic', headers={'Authorization': 'Basic <credentials>'})",
        'api:GET /bearer': "client.call('GET', '/bearer', headers={'Authorization': 'Bearer <token>'})",
        'api:GET /cookie': "client.call('GET', '/cookie', headers={'Cookie': 'sid=<api key>'})",
        'api:GET /header': "client.call('GET', '/header', headers={'x-goog-api-key': '<api key>'})",  # the first way
        'api:GET /oauth': "client.call('GET', '/oauth', headers={'Authorization': 'Bearer <token>'})",
        'api:GET /open': "client.call('GET', '/open')",  # anonymous calls are allowed
        'api:GET /query': "client.call('GET', '/query', query={'key': '<api key>'})"}
    respx_mock.route(host='api.test').respond(200)
    for identifier, example in examples.items():
        result = eval(example, {'client': c})  # the example runs as it is and satisfies the contract's security
        assert result.validation['backend_api']['request']['status'] == 'valid', identifier


def test_with_accounts_examples_sign_in_by_as_user(tmp_path, make_client):
    c = make_client(secured_contract(tmp_path), client_class=AccountsClient)
    assert c.describe('api:GET /header').example == "client.call('GET', '/header', as_user='alice')"


def varied_contract(tmp_path):
    """Operations whose examples need typed headers and cookies, schema bounds, media examples and other body types."""
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    ok = {'200': {'description': 'ok'}}
    obj = lambda properties: {'type': 'object', 'required': list(properties), 'properties': properties}
    body = lambda media, value: {'requestBody': {'required': True, 'content': {media: value}}, 'responses': ok}
    data['paths'] = {
        '/typed': {'get': {'responses': ok, 'parameters': [
            {'in': 'header', 'name': 'X-Page', 'required': True, 'schema': {'type': 'integer'}},
            {'in': 'header', 'name': 'X-Dry-Run', 'required': True, 'schema': {'type': 'boolean'}},
            {'in': 'cookie', 'name': 'step', 'required': True, 'schema': {'type': 'integer', 'minimum': 2}},
            {'in': 'query', 'name': 'limit', 'required': True, 'schema': {'type': 'integer', 'minimum': 5, 'maximum': 50}},
            {'in': 'query', 'name': 'share', 'required': True, 'schema': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1}}]}},
        '/bounded': {'post': body('application/json', {'schema': obj({
            'name': {'type': 'string', 'minLength': 10}, 'short': {'type': 'string', 'maxLength': 3},
            'count': {'type': 'integer', 'exclusiveMinimum': 1}, 'size': {'type': 'integer', 'minimum': 7, 'multipleOf': 5},
            'below': {'type': 'integer', 'maximum': -3}, 'tags': {'type': 'array', 'minItems': 2, 'items': {'type': 'string'}},
            'email': {'type': 'string', 'format': 'email', 'minLength': 3}})})},
        '/given': {'post': body('application/json', {'schema': obj({'name': {'type': 'string', 'pattern': '^[a-z]+$'}}),
                                                     'examples': {'first': {'value': {'name': 'given'}}}})},
        '/vendor': {'post': body('application/vnd.test+json', {'schema': obj({'id': {'type': 'integer'}})})},
        '/text': {'post': body('text/plain', {'schema': {'type': 'string'}})},
        '/xml': {'post': body('application/xml', {'schema': obj({'text': {'type': 'string'}})})},
        '/pattern': {'post': body('application/json', {'schema': obj({'code': {'type': 'string', 'pattern': '^[A-Z]{3}$'}})})},
        '/formats': {'post': body('application/json', {'schema': obj({
            'ip': {'type': 'string', 'format': 'ipv4'}, 'ip6': {'type': 'string', 'format': 'ipv6'},
            'blob': {'type': 'string', 'format': 'byte'}, 'at': {'type': 'string', 'format': 'time'},
            'mail': {'type': 'string', 'format': 'idn-email'}})})},
        '/steps': {'post': body('application/json', {'schema': obj({
            'price': {'type': 'number', 'minimum': 0.3, 'multipleOf': 0.1},
            'latitude': {'type': 'number', 'minimum': -90, 'maximum': 90, 'multipleOf': 0.0001},
            'ratio': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1, 'multipleOf': 0.05}})})},
        '/filtered': {'get': {'responses': ok, 'parameters': [
            {'in': 'query', 'name': 'filter', 'required': True, 'style': 'deepObject', 'explode': True,
             'schema': obj({'status': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 2}})},
            {'in': 'query', 'name': 'point', 'required': True, 'explode': False,
             'schema': obj({'x': {'type': 'integer'}, 'y': {'type': 'boolean'}})}]}},
        # openapi-core 0.23 does not read an exploded form object (from=1) and reports it missing: help only.
        '/ranged': {'get': {'responses': ok, 'parameters': [
            {'in': 'query', 'name': 'range', 'required': True, 'schema': obj({'from': {'type': 'integer'}})},
            {'in': 'query', 'name': 'box', 'required': True, 'style': 'spaceDelimited', 'explode': False,
             'schema': obj({'w': {'type': 'integer'}})}]}},
        '/unique': {'post': body('application/json', {'schema': obj({
            'tags': {'type': 'array', 'minItems': 3, 'uniqueItems': True, 'items': {'type': 'string', 'maxLength': 7}},
            'ids': {'type': 'array', 'minItems': 2, 'uniqueItems': True, 'items': {'type': 'string', 'format': 'uuid'}},
            'mails': {'type': 'array', 'minItems': 2, 'uniqueItems': True, 'items': {'type': 'string', 'format': 'email'}},
            'kinds': {'type': 'array', 'minItems': 2, 'uniqueItems': True, 'items': {'enum': ['a', 'b', 'c']}},
            'shares': {'type': 'array', 'minItems': 3, 'uniqueItems': True,
                       'items': {'type': 'number', 'maximum': 0.6, 'multipleOf': 0.1}}})})},
    }
    path.write_text(json.dumps(data))
    return path


def test_help_examples_run_as_they_are_and_satisfy_the_contract(tmp_path, make_client, respx_mock):
    c = make_client(varied_contract(tmp_path), validate=True)
    respx_mock.route(host='api.test').respond(200)
    examples = {op.id: c.describe(op.id).example for op in c.operations(kind='api')}
    statuses = {identifier: eval(example, {'client': c}).validation['backend_api']['request']['status']
                for identifier, example in examples.items()}  # no example raises
    assert {k: v for k, v in statuses.items() if v != 'valid'} == {
        'api:POST /pattern': 'invalid', 'api:POST /xml': 'invalid', 'api:GET /ranged': 'invalid'}
    # What capito cannot build is a visible placeholder, not a value that looks real.
    assert examples['api:POST /pattern'] == "client.call('POST', '/pattern', body={'code': '<pattern ^[A-Z]{3}$>'})"
    assert examples['api:POST /xml'] == "client.call('POST', '/xml', raw=b'<application/xml body>', content_type='application/xml')"
    assert "body={'name': 'given'}" in examples['api:POST /given']  # the contract's own example comes first
    # An object in the query is spread into the keys call() sends; a style it cannot spread is a placeholder.
    assert examples['api:GET /filtered'] == (
        "client.call('GET', '/filtered', query={'filter[status]': 'example', 'filter[limit]': 2, 'point': 'x,1,y,true'})")
    assert examples['api:GET /ranged'] == (
        "client.call('GET', '/ranged', query={'from': 1, 'box': '<box: an object, encode it by style spaceDelimited, explode false>'})")
