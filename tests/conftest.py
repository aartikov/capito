import pytest

from capito.client import Client
from capito.testing import config, no_network, put_session  # noqa: F401  (no_network: autouse fixture)
from helpers import TokenClient, answer, schema


@pytest.fixture
def make_client(tmp_path, respx_mock):
    """make_client(backend=None, analyst=None, replies=(), validate=False, client_class=Client, **config fields)."""
    def make(backend=None, analyst=None, replies=(), validate=False, client_class=Client, **fields):
        if replies:
            answer(respx_mock, *replies)
        return client_class(config(tmp_path, backend or schema(tmp_path), analyst, **fields), validate=validate)
    return make


@pytest.fixture
def make_robot(make_client):
    """A TokenClient with the account robot and a saved session; replies as in make_client."""
    def make(replies=(), *, access='old-access', refresh='old-refresh', **kwargs):
        c = make_client(replies=replies, client_class=TokenClient, **kwargs)
        c.accounts.create('robot')
        put_session(c, 'robot', {'access_token': access, 'refresh_token': refresh})
        return c
    return make


def sent(respx_mock):
    """The requests sent so far in this test (httpx.Request)."""
    return [call.request for call in respx_mock.calls]


@pytest.fixture
def requests(respx_mock):
    """sent() bound to this test: requests() -> [httpx.Request, ...]."""
    return lambda: sent(respx_mock)


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A directory with a tiny importable project module tiny_api and its capito.toml; returns (root, write)."""
    (tmp_path / 'tiny_api.py').write_text("import capito\n\n\nclass Client(capito.Client):\n    pass\n\n\nclass NotAClient:\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    schema(tmp_path)

    def write(client='tiny_api:Client'):
        key = f'client = "{client}"\n' if client else ''
        (tmp_path / 'capito.toml').write_text(key + 'default_environment = "dev"\n[environments.dev]\n'
                                              'base_url = "http://api.test"\n[environments.dev.contracts]\nmain = "api.json"\n')
        return tmp_path / 'capito.toml'

    write()
    return tmp_path, write
