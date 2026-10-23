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
