"""contracts.py: reading OpenAPI from a file or a URL, operations, schema notation."""
import json

import pytest

from capito.contracts import Contract, parse_document
from capito.transport import build_url
from helpers import schema


def contract(tmp_path, **parameters):
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['components'] = {'schemas': {'Thing': {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'integer'}}}}}
    data['paths'] = {'/things/{thingId}': {'parameters': [{'name': 'thingId', 'in': 'path', 'required': True, 'schema': {'type': 'string'}}],
        'get': {'operationId': 'getThing', 'parameters': list(parameters.values()), 'security': [],
                'responses': {'200': {'description': 'ok', 'content': {'application/json': {'schema': {'$ref': '#/components/schemas/Thing'}}}}}}}}
    path.write_text(json.dumps(data))
    c = Contract.from_file(path)
    c.path = path
    return c


def test_operations_resolve_by_id_and_route_and_keep_security(tmp_path):
    c = contract(tmp_path)
    op = c.get('getThing')
    assert op is c.get('GET', '/things/{thingId}')
    assert op.security == []
    assert c.expanded({'$ref': '#/components/schemas/Thing'})['properties']['id']['type'] == 'integer'
    with pytest.raises(ValueError, match='Unknown operation'):
        c.get('POST', '/unknown')


def test_schema_text_is_compact_and_defines_named_objects_once(tmp_path):
    c = contract(tmp_path)
    c.document['components']['schemas']['Node'] = {
        'type': 'object', 'required': ['id', 'kind'], 'description': 'Not repeated at each use',
        'properties': {
            'id': {'type': 'string', 'format': 'uuid'},
            'kind': {'enum': ['A', 'B', None]},
            'label': {'type': ['string', 'null'], 'maxLength': 10, 'description': 'Shown\n  on one line'},
            'tags': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}, 'maxItems': 3},
            'parent': {'$ref': '#/components/schemas/Node'},
            'thing': {'oneOf': [{'$ref': '#/components/schemas/Thing'}, {'type': 'null'}]},
            'other': {'$ref': '#/components/schemas/Thing'},
        }}
    assert c.schema_text({'$ref': '#/components/schemas/Node'}) == (
        'Node\n'
        'Node = {\n'
        '  id: uuid\n'
        '  kind: A|B | null\n'
        '  label?: string(maxLength=10) | null  # Shown on one line\n'
        '  tags?: integer(minimum=1)[](<=3)\n'
        '  parent?: Node\n'
        '  thing?: Thing | null\n'
        '  other?: Thing\n'
        '}\n'
        'Thing = {\n'
        '  id: integer\n'
        '}')
    assert c.schema_text({'type': 'object', 'additionalProperties': {'type': 'string'}}) == 'dict[string, string]'


def test_oas30_nullable_reads_as_null_like_a_null_type(tmp_path):
    c = contract(tmp_path)
    c.document['components']['schemas'] |= {
        'Owner': {'type': 'object', 'properties': {'id': {'type': 'string'}}},
        'Note': {'type': 'object', 'nullable': True, 'properties': {'text': {'type': 'string'}}},
        'Item': {'type': 'object', 'required': ['endedAt'], 'properties': {
            'endedAt': {'type': 'string', 'format': 'date-time', 'nullable': True},
            'startedAt': {'type': 'string', 'format': 'date-time', 'nullable': False},
            'owner': {'allOf': [{'$ref': '#/components/schemas/Owner'}], 'nullable': True},
            'note': {'$ref': '#/components/schemas/Note'},
            'tags': {'type': 'array', 'items': {'type': 'string'}, 'nullable': True}}}}
    assert c.schema_text({'$ref': '#/components/schemas/Item'}) == (
        'Item\n'
        'Item = {\n'
        '  endedAt: date-time | null\n'
        '  startedAt?: date-time\n'
        '  owner?: Owner | null\n'
        '  note?: Note | null\n'
        '  tags?: string[] | null\n'
        '}\n'
        'Owner = {\n'
        '  id?: string\n'
        '}\n'
        'Note = {\n'
        '  text?: string\n'
        '} | null')
    # past the depth limit the name keeps the mark too
    assert '  note?: Note | null' in c.schema_text({'$ref': '#/components/schemas/Item'}, depth=1).splitlines()


@pytest.mark.parametrize('enum, text', [
    ([True, False], 'true|false'),
    ([1, 2.5, 'a', None], '1|2.5|a | null'),
    (['DRAFT', 'ACTIVE'], 'DRAFT|ACTIVE'),
])
def test_enum_values_read_as_json(tmp_path, enum, text):
    assert contract(tmp_path).schema_text({'enum': enum}) == text


@pytest.mark.parametrize('value,encoded', [
    ('b c', 'q=b+c'), (False, 'q=false'), (3, 'q=3'), ([1, 2], 'q=1&q=2'), (('a', True), 'q=a&q=true'),
])
def test_path_and_query_encoding_needs_no_contract(value, encoded):
    assert build_url('/things/{thingId}', {'thingId': 'a/b ?'}, {'q': value}) == '/things/a%2Fb%20%3F?' + encoded
    with pytest.raises(ValueError, match='Path parameters missing'):
        build_url('/things/{thingId}', {}, {})
    with pytest.raises(TypeError, match='encode objects explicitly'):
        build_url('/things', {}, {'filter': {'a': 1}})


def test_path_parameter_overrides_and_duplicate_operation_ids(tmp_path):
    c = contract(tmp_path, key={'name': 'thingId', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}})
    assert c.get('getThing').parameters[0]['schema']['type'] == 'integer'
    data = c.document
    data['paths']['/other'] = {'get': {'operationId': 'getThing', 'responses': {}}}
    c.path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Duplicate operationId'):
        Contract.from_file(c.path)


@pytest.mark.parametrize('name,content', [('api.txt', '{}'), ('api.json', '{}'), ('api.yaml', 'paths: []'), ('api.json', '{bad')])
def test_unusable_help_contract_fails_help_not_client_creation(tmp_path, name, content, make_client):

    path = tmp_path / name
    path.write_text(content)
    c = make_client(path)  # nothing is read yet
    with pytest.raises(ValueError, match='Contract backend_api .* is unavailable'):
        c.operations()
    assert 'unavailable' in c.core.contracts()['backend_api']


@pytest.mark.parametrize('reference, document', [
    ('#/components/parameters/Missing', {}),                                 # a reference into nothing
    ('#/components/parameters/0', {'components': {'parameters': {}}}),      # a key that is not there
    ('#/x-shared/9', {'x-shared': [{'name': 'a', 'in': 'query'}]}),          # an index past the list
])
def test_a_broken_reference_makes_the_contract_unavailable_every_time(tmp_path, make_client, reference, document):
    path = schema(tmp_path)
    data = json.loads(path.read_text()) | document
    data['paths']['/things']['get']['parameters'] = [{'$ref': reference}]
    path.write_text(json.dumps(data))
    c = make_client(path)
    for _ in range(2):  # the second use gets the same reason, not a half-read contract
        with pytest.raises(ValueError, match=f'Contract backend_api .* is unavailable: Broken reference: {reference}'):
            c.operations()
    assert reference in c.core.contracts()['backend_api']['unavailable']


def test_a_reference_into_a_list_is_followed(tmp_path):
    path = schema(tmp_path)
    data = json.loads(path.read_text())
    data['x-shared'] = [{'name': 'limit', 'in': 'query', 'schema': {'type': 'integer'}}]
    data['paths']['/things']['get']['parameters'] = [{'$ref': '#/x-shared/0'}]
    path.write_text(json.dumps(data))
    assert [p['name'] for p in Contract.from_file(path).get('GET', '/things').parameters] == ['limit']


def test_format_is_recognised_by_content_not_by_name(tmp_path):
    source = schema(tmp_path, 'api.yaml')
    text = tmp_path / 'openapi'  # no extension, YAML inside
    text.write_text(source.read_text())
    assert Contract.from_file(text).get('GET', '/things')


def test_contract_from_url_is_fetched_once_through_the_client_transport(tmp_path, make_client, requests, respx_mock):
    served = schema(tmp_path).read_bytes()
    respx_mock.get('https://docs.example.test/openapi.json').respond(200, content=served, headers={'content-type': 'application/json'})
    c = make_client('https://docs.example.test/openapi.json')
    assert c.operations('things', kind='api') and c.describe('api:GET /things')
    assert [str(r.url) for r in requests()] == ['https://docs.example.test/openapi.json']
    info = c.core.contracts()['backend_api']
    assert info['source'] == 'https://docs.example.test/openapi.json' and len(info['sha256']) == 64


def test_failed_contract_download_is_reported(make_client, respx_mock):
    respx_mock.get('https://docs.example.test/openapi.json').respond(404)
    c = make_client('https://docs.example.test/openapi.json')
    with pytest.raises(ValueError, match='HTTP 404'):
        c.describe('api:GET /things')


def test_valid_json_is_read_as_json_even_where_yaml_disagrees():
    text = '{\n\t"openapi": "3.1.0",\n\t"paths": {"/things": {"get": {"parameters": [\n\t\t{"name": "limit", "in": "query", "schema": {"maximum": 1e5}}]}}}\n}'
    document = parse_document(text.encode())  # a tab in the indentation and an exponent
    assert document['paths']['/things']['get']['parameters'][0]['schema']['maximum'] == 100000


@pytest.mark.parametrize('text', ['openapi: 3.1.0\npaths: {}\n', '{openapi: 3.1.0, paths: {}}'])
def test_yaml_is_read_as_before(text):
    assert parse_document(text.encode()) == {'openapi': '3.1.0', 'paths': {}}


@pytest.mark.parametrize('text,message', [
    ('{\n  "openapi": "3.1.0",\n  "paths": {}\n', "api.json: not a valid JSON document: Expecting ',' delimiter at line 4 column 1"),
    ('openapi: 3.1.0\n\tpaths: {}\n', 'api.yaml: not a valid YAML document: .* at line 2 column 1'),
])
def test_a_broken_document_names_its_format_and_position(text, message):
    with pytest.raises(ValueError, match=message):
        parse_document(text.encode(), 'api.json' if text.startswith('{') else 'api.yaml')
