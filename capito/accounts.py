"""Local accounts: frozen records, the store with one reentrant lock per environment, and the accounts command set."""
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, final
import json
import os
import sys
import tempfile
import threading
import time
import uuid

from .commands import Commands
from .registry import command
from .results import Account, Secrets, thaw
from .view import ALL, ViewList

if sys.platform == 'win32':
    import msvcrt
else:
    import fcntl


class StaleSessionError(RuntimeError):
    pass


def freeze(value):
    """Read-only copy of JSON data: mappings become views that reject changes, lists become tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


@dataclass(frozen=True, repr=False)
class AccountRecord:
    """A stored account with credentials. For commands and adapters only; never print it.

    A snapshot: changing local state goes through client.core.save_session(), clear_session(),
    update_account() and accounts.update_local(), which check `revision` against newer changes.
    """
    id: str
    alias: str
    identity: Mapping[str, Any] = field(default_factory=dict)  # who it is; no secrets
    session: Mapping[str, Any] = field(default_factory=dict)   # credentials; their format belongs to the auth adapter
    settings: Mapping[str, Any] = field(default_factory=dict)  # local preferences
    revision: int = 0                                           # session revision

    def __post_init__(self):
        for name in ('identity', 'session', 'settings'):
            object.__setattr__(self, name, freeze(getattr(self, name)))

    @classmethod
    def from_json(cls, data):
        return cls(data['id'], data['alias'], data['identity'], data['session'], data['settings'], data['session_revision'])

    def to_json(self):
        return dict(id=self.id, alias=self.alias, identity=thaw(self.identity), session=thaw(self.session),
                    settings=thaw(self.settings), session_revision=self.revision)

    def __repr__(self):
        return f'<AccountRecord {self.alias}: credentials hidden>'


@dataclass(frozen=True, repr=False)
class Session:
    """An in-memory credential snapshot for call(session=...). Never refreshed or persisted by a call."""
    record: AccountRecord = field(repr=False)

    def __repr__(self):
        return '<Session: credentials hidden>'


class _Lock:
    def __init__(self):
        self.thread = threading.RLock()
        self.depth = 0
        self.fd = None


_locks = {}
_locks_guard = threading.Lock()


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)  # Windows ignores it: there the user profile's permissions protect the files


def replace_file(source, target, attempts=50):
    """os.replace; on Windows a reader holding the target open makes it fail for a moment, so retry briefly."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01)


def atomic_json(path, data):
    private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        replace_file(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def try_lock(fd):
    """One non-blocking attempt to lock the environment's lock file across processes."""
    try:
        if sys.platform == 'win32':
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, PermissionError, OSError) as exc:
        if isinstance(exc, (BlockingIOError, PermissionError)) or getattr(exc, 'errno', None) in (11, 13, 35, 36):
            return False
        raise


def unlock(fd):
    if sys.platform == 'win32':
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class AccountStore:
    """Account files of one environment. Internal to the client; projects use client.core and accounts methods."""
    def __init__(self, root, base_url, *, lock_timeout=30, policy=None, limits=ALL, summary_identity=(), lookup=()):
        self.root = Path(root).resolve()
        self.base_url = base_url
        self.lock_timeout = lock_timeout
        self.policy = policy or Secrets.http()
        self.limits = limits
        self.summary_identity = tuple(summary_identity)
        self.lookup = tuple(lookup)
        self._check_origin()

    def _check_origin(self):
        path = self.root / 'origin.json'
        if path.exists() and json.loads(path.read_text())['base_url'] != self.base_url:
            raise ValueError('State belongs to another server; choose a separate environment')

    @contextmanager
    def lock(self):
        with _locks_guard:
            state = _locks.setdefault((os.getpid(), str(self.root)), _Lock())
        deadline = time.monotonic() + self.lock_timeout
        if not state.thread.acquire(timeout=self.lock_timeout):
            raise TimeoutError('Timed out waiting for environment state lock')
        entered = False
        try:
            if state.depth == 0:
                private_dir(self.root)
                fd = os.open(self.root / '.lock', os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    while not try_lock(fd):
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Timed out waiting for environment state lock')
                        time.sleep(0.01)
                    self._check_origin()
                    if not (self.root / 'origin.json').exists():
                        atomic_json(self.root / 'origin.json', {'base_url': self.base_url})
                    for parent in (self.root, self.root / 'accounts'):
                        for leftover in parent.glob('.tmp-*'):
                            leftover.unlink()
                    state.fd = fd
                except BaseException:
                    os.close(fd)
                    raise
            state.depth += 1
            entered = True
            yield
        finally:
            if entered:
                state.depth -= 1
                if state.depth == 0:
                    unlock(state.fd)
                    os.close(state.fd)
                    state.fd = None
            state.thread.release()

    def _path(self, account_id):
        return self.root / 'accounts' / (str(uuid.UUID(account_id)) + '.json')

    def _write(self, record):
        atomic_json(self._path(record.id), record.to_json())

    def records(self):
        self._check_origin()
        records = []
        for path in sorted((self.root / 'accounts').glob('*.json')):
            try:
                records.append(AccountRecord.from_json(json.loads(path.read_text())))
            except FileNotFoundError:
                continue  # Another process forgot this account after directory listing.
        return records

    def resolve(self, selector):
        """The record whose ID, alias (any case) or one of the lookup identity fields equals selector."""
        text = str(selector)
        matches = [r for r in self.records()
                   if r.id == text or r.alias.casefold() == text.casefold()
                   or any(r.identity.get(name) == selector for name in self.lookup)]
        if len(matches) != 1:
            raise ValueError(f'Unknown or ambiguous account: {selector!r}')
        return matches[0]

    def _check_unique(self, alias, identity, skip=None):
        for other in self.records():
            if other.id == skip:
                continue
            if alias is not None and other.alias.casefold() == alias.casefold():
                raise ValueError(f'Alias already exists: {alias}')
            for name in self.lookup:
                value = identity.get(name)
                if value is not None and other.identity.get(name) == value:
                    raise ValueError(f'{name} {value!r} already belongs to account {other.alias!r}')

    def create(self, alias, identity=None, settings=None):
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError('A nonempty local alias is required')
        with self.lock():
            self._check_unique(alias, identity or {})
            record = AccountRecord(str(uuid.uuid4()), alias, identity or {}, {}, settings or {}, 0)
            self._write(record)
            return record

    def update(self, account, *, identity=None, settings=None, revision=None):
        """Merge identity/settings changes; with revision, refuse if the session changed since that snapshot."""
        with self.lock():
            try:
                current = self.resolve(account if isinstance(account, str) else account.id)
            except ValueError:
                raise StaleSessionError('Account was removed meanwhile') from None
            if revision is not None and current.revision != revision:
                raise StaleSessionError('Session changed meanwhile')
            new_identity = {**thaw(current.identity), **(identity or {})}
            if identity:
                self._check_unique(None, {k: v for k, v in identity.items() if k in self.lookup}, skip=current.id)
            updated = AccountRecord(current.id, current.alias, new_identity, current.session,
                                    {**thaw(current.settings), **(settings or {})}, current.revision)
            self._write(updated)
            return updated

    def replace_session(self, snapshot, session):
        with self.lock():
            try:
                current = self.resolve(snapshot.id)
            except ValueError:
                raise StaleSessionError('Account was removed while request was running') from None
            if current.revision != snapshot.revision:
                raise StaleSessionError('Session changed while request was running')
            updated = AccountRecord(current.id, current.alias, current.identity, session, current.settings,
                                    current.revision + 1)
            self._write(updated)
            return updated

    def forget(self, account_id):
        with self.lock():
            record = self.resolve(account_id)
            self._path(record.id).unlink()

    def public(self, record):
        """The record without credentials, as shown to callers."""
        return Account(record, policy=self.policy, limits=self.limits, summary_identity=self.summary_identity)


# Arguments of the local account commands; project commands reuse them explicitly.
ACCOUNT_ARGUMENTS = {
    'alias': {'type': 'str', 'description': 'Stable unique local alias; not the name of the server profile.', 'example': 'qa_new'},
    'selector': {'type': 'str', 'description': 'ID, alias or a project lookup field value of an existing local record.', 'example': 'alice'},
    'identity': {'type': 'dict | None', 'description': 'Account identifiers in the project model. Not a cache of the server profile.', 'example': {}},
    'settings': {'type': 'dict | None', 'description': 'Explicit local account settings, e.g. file bindings.', 'example': {}},
}


def account_arguments(*names, **extra):
    """Help metadata for the named account arguments, plus command-specific ones."""
    return {**{name: ACCOUNT_ARGUMENTS[name] for name in names}, **extra}


class AccountsCommands(Commands):
    """The 'accounts' command set: local account records, and how as_user finds them.

    Declared in Client.commands under the key 'accounts' (this class or a subclass); without it the client
    has no accounts and as_user is unavailable.
    lookup: identity fields accepted as selectors besides ID and alias; their values are unique.
    summary_identity: identity fields shown by print(account).
    A create() override with domain arguments must delegate to super().create(), which checks uniqueness.
    """
    lookup: ClassVar[tuple[str, ...]] = ()
    summary_identity: ClassVar[tuple[str, ...]] = ()

    @final
    def resolve(self, selector) -> AccountRecord:
        """The stored record, credentials included; for commands and adapters only — never print it."""
        return self.client.core._store.resolve(selector)

    @final
    def update_local(self, selector, *, identity=None, settings=None) -> Account:
        """Change identity/settings without a server request, e.g. local file bindings. Not a command."""
        return self.client.core._store.public(self.client.core._store.update(self.resolve(selector), identity=identity, settings=settings))

    @command('List local accounts without credentials; no server requests',
             result='accounts',
             effects='Reads local records; no server requests, no session changes.')
    def list(self):
        store = self.client.core._store
        return ViewList((store.public(r) for r in store.records()), limits=self.client.core.config.view['list'])

    @command('Read one local account without credentials',
             result='account',
             effects='Reads a local record; no server requests, no session changes.',
             parameters=account_arguments('selector'))
    def get(self, selector):
        return self.client.core._store.public(self.resolve(selector))

    @command('Create a local account record; does not register on the server',
             result='account',
             effects='Creates only a local record. Neither logs in nor registers on the server.',
             parameters=account_arguments('alias', 'identity', 'settings'))
    def create(self, alias, *, identity=None, settings=None):
        return self.client.core._store.public(self.client.core._store.create(alias, identity, settings))

    @command('Forget a local account; no logout or server deletion',
             result='none',
             effects='Removes the local record and its saved session. Nothing changes on the server.',
             parameters=account_arguments('selector'))
    def forget(self, selector):
        self.client.core._store.forget(self.resolve(selector).id)

    @command('Capture current session in memory for calls without refresh',
             result='session',
             effects='Reads a local record; no server requests, no session changes.',
             parameters=account_arguments('selector'))
    def session(self, selector):
        return Session(self.resolve(selector))
