"""accounts.py: stored records, revisions, the environment lock across threads and processes, and the accounts command set."""
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from capito.accounts import AccountStore, StaleSessionError
from helpers import AccountsClient


def test_stale_response_cannot_resurrect_or_overwrite(make_client):
    store = make_client(client_class=AccountsClient).core._store
    old = store.create('robot')
    store.replace_session(old, {'access': 'new'})
    with pytest.raises(StaleSessionError):
        store.replace_session(old, {})
    newer = store.resolve(old.id)
    store.forget(old.id)
    with pytest.raises(StaleSessionError):
        store.replace_session(newer, {'access': 'resurrect'})


def test_no_lost_settings_updates_and_reentrant_lock(make_client):
    c = make_client(client_class=AccountsClient)
    c.accounts.create('robot')
    def update(n):
        with c.core._store.lock():
            c.accounts.update_local('robot', settings={str(n): n})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(50)))
    assert len(c.accounts.resolve('robot').settings) == 50
    assert len(list(c.core.config.state.glob('*.lock'))) == 1
    assert not list(c.core.config.state.rglob('.tmp-*'))


def test_origin_binding(make_client):
    c = make_client(client_class=AccountsClient)
    c.accounts.create('robot')
    with pytest.raises(ValueError):
        AccountStore(c.core.config.state, 'https://elsewhere.example')


def test_records_are_frozen_snapshots(make_client):
    c = make_client(client_class=AccountsClient)
    c.accounts.create('robot', identity={'tags': ['a']})
    record = c.accounts.resolve('robot')
    with pytest.raises(TypeError):
        record.identity['tags'] = []
    with pytest.raises(AttributeError):
        record.alias = 'other'
    assert 'robot' in repr(record) and 'hidden' in repr(record)


def child(code, *args):
    return subprocess.Popen([sys.executable, '-c', code, *map(str, args)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_process_exit_releases_lock_and_cleans_temporary_files(tmp_path):
    root = tmp_path / 'state'
    script = '''from capito.accounts import AccountStore
import sys, os
s = AccountStore(sys.argv[1], "http://localhost")
with s.lock():
    (s.root / ".tmp-interrupted").write_text("incomplete")
    os._exit(0)
'''
    assert child(script, root).wait(timeout=5) == 0
    with AccountStore(root, 'http://localhost').lock():
        assert not list(root.glob('.tmp-*'))
    assert len(list(root.glob('.lock'))) == 1


def test_interprocess_updates_do_not_get_lost(tmp_path):
    store = AccountStore(tmp_path / 'state', 'http://localhost')
    account = store.create('robot')
    script = '''from capito.accounts import AccountStore
import sys
s = AccountStore(sys.argv[1], "http://localhost")
for i in range(15):
    s.update(sys.argv[2], settings={sys.argv[3] + str(i): i})
'''
    processes = [child(script, store.root, account.id, f'p{n}-') for n in range(3)]
    for process in processes:
        _, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
    assert len(store.resolve('robot').settings) == 45


def test_lock_timeout(tmp_path):
    store = AccountStore(tmp_path / 'state', 'http://localhost', lock_timeout=.1)
    script = '''from capito.accounts import AccountStore
import sys, time
s = AccountStore(sys.argv[1], "http://localhost")
with s.lock():
    print("locked", flush=True)
    time.sleep(5)
'''
    process = child(script, store.root)
    try:
        assert process.stdout.readline().strip() == 'locked'
        with pytest.raises(TimeoutError):
            with store.lock():
                pass
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_lookup_fields_resolve_accounts_and_stay_unique(make_client):
    from capito.client import Client
    from capito.accounts import AccountsCommands

    class ByEmail(AccountsCommands):
        lookup = ('email',)
        summary_identity = ('email',)

    c = make_client(client_class=type('C', (Client,), {'commands': {'accounts': ByEmail}}))
    c.accounts.create('robot', identity={'email': 'r@example.test'})
    assert c.accounts.resolve('r@example.test').alias == 'robot'
    assert str(c.accounts.get('robot')) == 'robot  email=r@example.test  session=no'
    with pytest.raises(ValueError, match='already belongs'):
        c.accounts.create('twin', identity={'email': 'r@example.test'})
    c.accounts.create('other', identity={'email': 'o@example.test'})
    with pytest.raises(ValueError, match='already belongs'):
        c.accounts.update_local('other', identity={'email': 'r@example.test'})
    with pytest.raises(ValueError, match='Alias already exists'):
        c.accounts.create('ROBOT')
