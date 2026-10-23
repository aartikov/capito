"""core.py: the Core declaration, local account changes and session refresh."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from capito.testing import put_session
from helpers import BASE_URL, TokenClient


def test_core_declaration_is_checked_and_keeps_mechanisms_apart_from_command_sets(make_client):
    from capito.client import Client
    from capito.commands import Commands
    from capito.core import Core
    from helpers import TokenAuth

    with pytest.raises(TypeError, match='subclass of capito.Auth'):
        Core(auth=str)
    with pytest.raises(TypeError, match='Secrets policy'):
        Core(secrets={'keys': ()})
    with pytest.raises(TypeError, match='Client.core must be capito.Core'):
        make_client(client_class=type('C', (Client,), {'core': TokenAuth}))
    with pytest.raises(TypeError, match='would hide client.core'):
        make_client(client_class=type('C', (Client,), {'commands': {'core': Commands}}))

    class AuthCommands(Commands):
        pass

    class ConfigCommands(Commands):
        pass

    # Names of mechanisms are free for command sets: they live in client.core.
    c = make_client(client_class=type('C', (Client,), {'core': Core(auth=TokenAuth),
                                                      'commands': {'auth': AuthCommands, 'config': ConfigCommands}}))
    assert isinstance(c.auth, AuthCommands) and isinstance(c.core.auth, TokenAuth)
    assert isinstance(c.config, ConfigCommands) and c.core.config.environment == 'test'


def test_save_session_stores_tokens_from_a_successful_response(make_robot):
    c = make_robot([(200, {'access': 'acc-2', 'refresh': 'ref-2'})])
    account = c.accounts.resolve('robot')
    result = c.core.save_session(c.call('POST', '/refresh', body={}), account, identity={'server_id': None})
    assert result.success and 'acc-2' not in str(result.view('body', depth='all'))
    stored = c.accounts.resolve('robot')
    assert c.core.auth.tokens(stored).access == 'acc-2' and stored.revision == account.revision + 1
    assert stored.identity['server_id'] is None


def test_failed_or_tokenless_responses_change_nothing(make_robot):
    c = make_robot([(401, {}), (200, {'access': 'only-access'})])
    account = c.accounts.resolve('robot')
    assert not c.core.save_session(c.call('POST', '/refresh', body={}), account).success
    tokenless = c.core.save_session(c.call('POST', '/refresh', body={}), account)
    assert not tokenless.success and 'no usable session' in tokenless.error['message']
    assert c.accounts.resolve('robot').revision == account.revision


def test_a_concurrent_change_wins_and_the_late_result_fails(make_robot):
    c = make_robot([(200, {'access': 'late', 'refresh': 'late-r'})])
    snapshot = c.accounts.resolve('robot')
    put_session(c, 'robot', {'access_token': 'newer', 'refresh_token': 'newer-r'})
    late = c.core.save_session(c.call('POST', '/refresh', body={}), snapshot)
    assert not late.success and 'changed' in late.error['message']
    assert c.core.auth.tokens(c.accounts.resolve('robot')).access == 'newer'


def test_account_defaults_to_the_one_the_request_was_sent_as(make_robot):
    c = make_robot([(200, {'id': 1}), (200, {'id': 2}), (200, {})])
    sent = c.call('GET', '/things', as_user='robot')
    assert c.core.update_account(sent, identity={'server_id': 's-1'}).success
    assert c.accounts.resolve('robot').identity['server_id'] == 's-1'
    assert c.core.clear_session(c.call('GET', '/things', as_user='robot')).success
    assert not c.accounts.resolve('robot').session
    with pytest.raises(ValueError, match='Pass the account'):
        c.core.update_account(c.call('GET', '/things'), identity={'server_id': 's-2'})


def test_update_account_refuses_a_stale_or_removed_record(make_robot):
    c = make_robot([(200, {}), (200, {})])
    snapshot = c.accounts.resolve('robot')
    put_session(c, 'robot', {'access_token': 'newer', 'refresh_token': 'r'})
    stale = c.core.update_account(c.call('GET', '/things'), snapshot, identity={'server_id': 'x'})
    assert not stale.success and 'server_id' not in c.accounts.resolve('robot').identity
    fresh = c.accounts.resolve('robot')
    c.accounts.forget('robot')
    assert not c.core.update_account(c.call('GET', '/things'), fresh, identity={'server_id': 'x'}).success


def test_failure_is_a_failed_local_result(make_robot):
    c = make_robot()
    result = c.core.failure('Event has no id')
    assert not result.success and result.status is None and result.error == {'code': None, 'message': 'Event has no id'}
    assert str(result) == '— · Event has no id'


TOKENS = {'access': 'new', 'refresh': 'new-r'}


def test_parallel_requests_share_a_single_refresh(make_robot, respx_mock):
    c = make_robot()
    barrier = threading.Barrier(4)

    def refresh(request):
        time.sleep(.02)
        return httpx.Response(200, json=TOKENS)

    def things(request):
        if request.headers['authorization'] == 'Bearer old-access':
            barrier.wait(timeout=3)
            return httpx.Response(401, json={})
        return httpx.Response(200, json={'id': 1})

    refreshes = respx_mock.post(f'{BASE_URL}/refresh').mock(side_effect=refresh)
    respx_mock.get(f'{BASE_URL}/things').mock(side_effect=things)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: TokenClient(c.core.config).call('GET', '/things', as_user='robot'), range(4)))
    assert all(r.success for r in results)
    assert refreshes.call_count == 1


def test_direct_refreshes_are_serialized(make_robot, respx_mock):
    c = make_robot()
    active = peak = 0
    mutex = threading.Lock()

    def refresh(request):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(peak, active)
        time.sleep(.02)
        with mutex:
            active -= 1
        return httpx.Response(200, json=TOKENS)

    respx_mock.post(f'{BASE_URL}/refresh').mock(side_effect=refresh)

    def run(_):
        other = TokenClient(c.core.config)
        return other.core.refresh_session(other.accounts.resolve('robot'))

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(3)))
    assert peak == 1 and all(r.success for r in results)
