"""Authorization adapters: the interface the client calls, and the ready Bearer + refresh scheme.

Adapters are stateless and may be called from several threads; all state lives in the account store.
"""
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, final

if TYPE_CHECKING:
    from .accounts import AccountRecord
    from .client import Client
    from .results import Result
    from .transport import Request, Response


def header(headers, name):
    """Case-insensitive header lookup; name is lowercase."""
    return next((v for k, v in headers.items() if k.lower() == name), None)


def path_value(data, path):
    """The value at a dotted path such as 'data.tokens.access', or None."""
    for part in path.split('.'):
        if not isinstance(data, Mapping) or part not in data:
            return None
        data = data[part]
    return data


class Auth:
    """No authorization. Base of all adapters: the client calls only these methods."""

    def has_credentials(self, request: 'Request') -> bool:
        """True if the caller attached authorization explicitly; the client then neither prepares nor refreshes."""
        return False

    def prepare(self, request: 'Request', account: 'AccountRecord') -> None:
        """Attach the account's credentials to request.headers. Called before every attempt; no I/O, no state."""

    def should_refresh(self, request: 'Request', response: 'Response') -> bool:
        """Whether this response means 'credentials expired'. Pure: no I/O, no state."""
        return False

    def refresh(self, client: 'Client', account: 'AccountRecord', *, validate: bool | None = None) -> 'Result':
        """Obtain and save a new session; the client calls it under the environment lock with a fresh record."""
        return client.core.failure('This API has no session refresh')

    def session_from(self, result: 'Result') -> Mapping[str, Any] | None:
        """The session to store from a successful auth response; None if the response holds none."""
        raise TypeError(f'{type(self).__name__} does not store sessions')


@dataclass(frozen=True, repr=False)
class BearerTokens:
    access: str | None
    refresh: str | None

    def __repr__(self):
        return '<BearerTokens: hidden>'


class BearerAuth(Auth, ABC):
    """Authorization: Bearer <access token>; one refresh after 401. The stored session format is private.

    A project declares where its auth responses keep the tokens and how to send its refresh request:

        class MyAuth(BearerAuth):
            access_token_path = 'accessToken'
            refresh_token_path = 'refreshToken'

            def send_refresh(self, client, refresh_token, account, *, validate):
                return client.call('POST', '/auth/refresh', body={'refreshToken': refresh_token}, validate=validate)
    """
    access_token_path: ClassVar[str]   # dotted path in the auth response body
    refresh_token_path: ClassVar[str]

    def __init__(self):
        for name in ('access_token_path', 'refresh_token_path'):
            value = getattr(type(self), name, None)
            if not isinstance(value, str) or not value:
                raise TypeError(f'{type(self).__name__} must declare {name}')

    @abstractmethod
    def send_refresh(self, client: 'Client', refresh_token: str, account: 'AccountRecord',
                     *, validate: bool | None) -> 'Result':
        """The project's refresh request, without as_user. Return the raw result; tokens are extracted and saved here."""

    def should_refresh(self, request, response):
        """Override if the API reports expired tokens other than by 401."""
        return response.status == 401

    @final
    def has_credentials(self, request):
        return header(request.headers, 'authorization') is not None

    @final
    def prepare(self, request, account):
        token = account.session.get('access_token')
        if token:
            request.headers['Authorization'] = 'Bearer ' + token

    @final
    def refresh(self, client, account, *, validate=None):
        token = account.session.get('refresh_token')
        if not token:
            return client.core.failure('No refresh token; explicit login required')
        result = self.send_refresh(client, token, account, validate=validate)
        result.add_secrets(token)
        return client.core.save_session(result, account)

    @final
    def session_from(self, result):
        body = result.body
        access, refresh = path_value(body, self.access_token_path), path_value(body, self.refresh_token_path)
        if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh):
            return None
        return {'access_token': access, 'refresh_token': refresh}

    @final
    def tokens(self, account: 'AccountRecord') -> BearerTokens | None:
        """The account's tokens, e.g. for a logout body; None without a session."""
        if not account.session:
            return None
        return BearerTokens(account.session.get('access_token'), account.session.get('refresh_token'))
