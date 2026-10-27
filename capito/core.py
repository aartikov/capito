"""capito's own mechanisms of a client: declared as Client.core = Core(...), running as client.core.

Kept apart from the project's command sets (client.<set>) so that new mechanisms never collide with them.
"""
from dataclasses import dataclass, field
import threading
from typing import Callable

from .accounts import AccountStore, StaleSessionError
from .auth import Auth
from .contracts import ContractSource
from .registry import Registry
from .results import CompoundResult, Result, Secrets, error_from, strings
from .transport import Request
from .validation import Validator


def no_error_format(body):
    """(code, message) of a failed response body when the API has no declared error format."""
    return None, None


def fail(result, message):
    """Mark a result failed by a local reason: success False, the reason in errors and error."""
    result.errors.append(message)
    result.success = False
    result.error = error_from(result.errors)
    return result


@dataclass(frozen=True)
class Core:
    """What a project declares about capito's mechanisms; all fields are optional.

    auth: the authorization adapter class (default: no authorization); client.core.auth is its instance.
    secrets: what view() hides; extend Secrets.http() rather than replace it.
    error_parser: (code, message) of a failed response body, filling result.error.
    """
    auth: type = Auth
    secrets: Secrets = field(default_factory=Secrets.http)
    error_parser: Callable = no_error_format

    def __post_init__(self):
        if not (isinstance(self.auth, type) and issubclass(self.auth, Auth)):
            raise TypeError('Core(auth=...) must be a subclass of capito.Auth')
        if not isinstance(self.secrets, Secrets):
            raise TypeError('Core(secrets=...) must be a Secrets policy, e.g. Secrets.http().extend(...)')
        if not callable(self.error_parser):
            raise TypeError('Core(error_parser=...) must be a function of the response body')


class CoreRuntime:
    """client.core: configuration, transport, authorization, help registry and local account changes.

    For command and adapter code; scenarios use client.call(), client.<set> and help.
    """
    def __init__(self, client, declaration, config, transport, validate, accounts):
        self.config = config
        self.transport = transport
        self.validate = validate
        self.secrets = declaration.secrets
        self.error_parser = declaration.error_parser
        self.auth = declaration.auth()
        self._client = client
        self._local = threading.local()
        self._sources = {name: ContractSource(name, location, self._fetch_contract)
                         for name, location in config.contracts.items()}
        self._validators = {name: Validator(source, config.base_url) for name, source in self._sources.items()}
        self._store = AccountStore(config.state, config.base_url, policy=self.secrets, limits=config.view['account'],
                                   summary_identity=accounts.summary_identity if accounts else (),
                                   lookup=accounts.lookup if accounts else ())
        self.registry = Registry(lambda: self._sources[config.help_contract].load(),
                                 help_limits=config.view['help'], list_limits=config.view['list'],
                                 accounts=accounts is not None)

    def __repr__(self):
        return f'<capito core of {type(self._client).__name__}: auth={type(self.auth).__name__}>'

    @property
    def contract(self):
        """The help contract (help_contract of the configuration), read on first access."""
        return self.registry.contract

    def contracts(self):
        """Provenance of the configured contracts; does not claim the server matches them."""
        return {name: source.info() for name, source in self._sources.items()}

    def _fetch_contract(self, url):
        response = self.transport.send(Request('GET', url, {'Accept': 'application/yaml, application/json'}))
        if response.error or response.status != 200:
            raise OSError(response.error or f'HTTP {response.status}')
        return response.content

    def compound(self, name):
        """An empty result of a multi-step command, with the client's secret policy and view limits."""
        return CompoundResult(name, policy=self.secrets, limits=self.config.view['result'])

    def failure(self, message):
        """A failed local outcome without a request, e.g. a missing precondition found by a command."""
        return fail(Result(policy=self.secrets, limits=self.config.view['result']), message)

    def result(self, **fields):
        """A Result with the client's secret policy and view limits."""
        return Result(policy=self.secrets, limits=self.config.view['result'], **fields)

    def refresh_session(self, account, *, validate=None):
        """Refresh a saved account's session under the environment lock.

        If another worker already refreshed it, the newer session is reused without a request.
        """
        with self._store.lock():
            try:
                latest = self._store.resolve(account.id)
            except ValueError:
                return self.failure('Account was removed; no refresh performed')
            if latest.revision != account.revision:
                if not latest.session:
                    return self.failure('Session was cleared')
                return self.result(success=True, reused_concurrent_refresh=True)
            self._local.refreshing = True
            try:
                return self.auth.refresh(self._client, latest, validate=validate)
            finally:
                self._local.refreshing = False

    @property
    def refreshing(self):
        """True inside the adapter's refresh on this thread; requests with as_user are refused there."""
        return getattr(self._local, 'refreshing', False)

    def _account_for(self, result, account):
        account = account if account is not None else result.sent_account
        if account is None:
            raise ValueError('Pass the account the request was made for: the result was not sent as_user')
        return account

    def _local_change(self, result, change):
        """Run change() under the state lock if result succeeded; a concurrent change becomes a result error."""
        if not result.success:
            return result
        try:
            with self._store.lock():
                change()
        except StaleSessionError as exc:
            fail(result, str(exc))
        return result

    def save_session(self, result, account=None, *, identity=None):
        """After a successful auth response, store its session in the account; identity changes go with it.

        account defaults to result.sent_account. A response without a usable session, a concurrent session
        change or a removed account makes the result failed. Returns the same result.
        """
        account = self._account_for(result, account)
        if not result.success:
            return result
        session = self.auth.session_from(result)
        if not session:
            return fail(result, 'Successful auth response has no usable session')
        result.add_secrets(*strings(session))
        def change():
            updated = self._store.replace_session(account, session)
            if identity:
                self._store.update(updated, identity=identity, revision=updated.revision)
        return self._local_change(result, change)

    def clear_session(self, result, account=None, *, identity=None):
        """After a successful logout or deletion, drop the account's session; same rules as save_session()."""
        account = self._account_for(result, account)
        def change():
            updated = self._store.replace_session(account, {})
            if identity:
                self._store.update(updated, identity=identity, revision=updated.revision)
        return self._local_change(result, change)

    def update_account(self, result, account=None, *, identity=None, settings=None):
        """After a successful response, merge identity/settings into the account, e.g. a server ID it reported."""
        account = self._account_for(result, account)
        return self._local_change(result, lambda: self._store.update(account, identity=identity, settings=settings,
                                                                     revision=account.revision))
