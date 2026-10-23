"""Authorization adapters: the base interface, BearerAuth declarations and the refresh guard."""
import pytest

from capito.auth import Auth, BearerAuth
from capito.client import Client
from capito.accounts import AccountsCommands
from capito.core import Core
from capito.testing import put_session
from helpers import AccountsClient


class KeyAuth(Auth):
    def prepare(self, request, account):
        request.headers['X-Service-Key'] = account.session['credential']


class KeyClient(Client):
    core = Core(auth=KeyAuth)
    commands = {'accounts': AccountsCommands}


def test_generic_adapter_with_non_phone_identity(make_client, requests):
    c = make_client(replies=[(200, {'id': 1})], client_class=KeyClient)
    c.accounts.create('robot', identity={'service_name': 'qa'})
    put_session(c, 'robot', {'credential': 'secret-service-value'})
    result = c.call('POST', '/things', as_user='robot', body={'id': 1})
    assert result.success
    assert requests()[0].headers['X-Service-Key'] == 'secret-service-value'
    assert 'secret-service-value' not in str(result)


def test_base_adapter_has_no_refresh_and_stores_no_session(make_client):
    c = make_client(replies=[(200, {'id': 1})], client_class=AccountsClient)
    c.accounts.create('robot')
    result = c.call('GET', '/things', as_user='robot')
    with pytest.raises(TypeError, match='does not store sessions'):
        c.core.save_session(result)
    assert not c.core.refresh_session(c.accounts.resolve('robot')).success


def test_bearer_declaration_errors_surface_when_the_client_is_created(make_client):
    class NoPaths(BearerAuth):
        def send_refresh(self, client, refresh_token, account, *, validate):
            return client.core.failure('unused')

    class NoRefresh(BearerAuth):
        access_token_path = 'a'
        refresh_token_path = 'r'

    for adapter, message in ((NoPaths, 'must declare access_token_path'), (NoRefresh, 'abstract')):
        client_class = type('C', (Client,), {'core': Core(auth=adapter)})
        with pytest.raises(TypeError, match=message):
            make_client(client_class=client_class)


def test_bearer_reads_nested_token_paths_and_hides_tokens(make_client):
    class Nested(BearerAuth):
        access_token_path = 'data.tokens.access'
        refresh_token_path = 'data.tokens.refresh'

        def send_refresh(self, client, refresh_token, account, *, validate):
            return client.core.failure('unused')

    client_class = type('C', (Client,), {'core': Core(auth=Nested), 'commands': {'accounts': AccountsCommands}})
    c = make_client(replies=[(200, {'data': {'tokens': {'access': 'acc-1', 'refresh': 'ref-1'}}})], client_class=client_class)
    c.accounts.create('robot')
    account = c.accounts.resolve('robot')
    result = c.core.save_session(c.call('POST', '/things', body={'id': 1}), account)
    assert result.success and 'acc-1' not in str(result.view('body', depth='all'))
    tokens = c.core.auth.tokens(c.accounts.resolve('robot'))
    assert (tokens.access, tokens.refresh) == ('acc-1', 'ref-1') and 'acc-1' not in repr(tokens)


def test_refresh_requests_must_not_use_as_user(make_robot):
    class Recursive(BearerAuth):
        access_token_path = 'access'
        refresh_token_path = 'refresh'

        def send_refresh(self, client, refresh_token, account, *, validate):
            return client.call('POST', '/refresh', as_user=account.alias)

    c = make_robot([(401, {})])
    c.core.auth = Recursive()
    with pytest.raises(RuntimeError, match='without as_user'):
        c.call('GET', '/things', as_user='robot')
