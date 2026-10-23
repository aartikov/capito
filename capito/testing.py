"""Test helpers for projects on capito (requires pytest and respx: pip install capito[test]).

Import the fixture into conftest.py so that it applies to every test:

    from capito.testing import no_network  # noqa: F401  (autouse fixture)

Responses come from respx routes, e.g. respx_mock.post(f'{BASE_URL}/items').respond(201, json={'id': 1}).
"""
import pytest

from capito.config import Config


@pytest.fixture(autouse=True)
def no_network(respx_mock):
    """Every HTTP request of a test must match a respx route; anything else fails the test.

    respx also fails a test whose route was never called. A test with a real loopback server adds
    respx_mock.route(host='127.0.0.1').pass_through().
    """
    yield respx_mock


def config(tmp_path, contract, analyst=None, *, base_url='http://api.test', **fields):
    """A configuration on a temporary directory: contracts backend_api (help) and optional analyst_api; view limits 'all' unless given."""
    contracts = {'backend_api': contract}
    if analyst is not None:
        contracts['analyst_api'] = analyst
    return Config(root=tmp_path, environment='test', base_url=base_url, contracts=contracts,
                  help_contract='backend_api', **fields)


def put_session(client, selector, session, *, identity=None):
    """Test setup: store a session (in the adapter's format) and identity fields for an existing account."""
    store = client.core._store
    record = store.replace_session(store.resolve(selector), session)
    if identity:
        record = store.update(record, identity=identity)
    return record
