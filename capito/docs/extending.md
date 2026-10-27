# Connecting your API

capito is a ready tool that knows nothing about any particular API. A project extends it with a package next to itself: a `Client` class, commands, an account model and authorization. How to use a ready client: `python -m capito docs usage`.

## What a project needs

```
my-testing/
├── capito.toml            client = "my_api:Client", environments, contracts, [view]
├── pyproject.toml         capito dependency; the my_api package
├── requirements.lock      capito==X.Y.Z (or -e ../capito during development)
├── api/backend_api.yaml   the contract, if it is kept in the project
└── my_api/
    ├── __init__.py        class Client(capito.Client)
    ├── auth.py            authorization adapter
    ├── commands/          command sets: client.<name> is the class <Name>Commands in commands/<name>.py
    │   ├── __init__.py    exports the set classes
    │   ├── accounts.py    AccountsCommands: account model and local commands
    │   ├── auth.py        AuthCommands: sign-in and logout
    │   └── items.py       ItemsCommands
    └── tests/
```

**A command set** is a subclass of `capito.Commands` with `@command` methods; the client exposes it as `client.<name>`, and in help its commands are `command:<name>.<command>`. One naming rule for all sets: the key in `commands` is the set name (`client.items`), the class is `<Name>Commands`, the module is `commands/<name>.py`. The class name shows at once which set it belongs to, and names do not clash with capito classes (`Auth`).

Installation: `.venv/bin/python -m pip install -r requirements.lock`, then `.venv/bin/python -m pip install --no-deps -e .`; the project package then imports from any script, and `python -m capito` finds the client by the `client` key. A project with its own client must set this key: without it the config means the base `capito.Client`, and `my_api.Client.from_config()` refuses, so that code and CLI do not diverge.

## The `Client` class

```python
import capito
from capito import Core, Secrets
from .auth import MyAuth
from .commands import AccountsCommands, AuthCommands, ItemsCommands


def parse_error(body):                        # (code, message) of a failure for result.error
    if not isinstance(body, dict):            # the body may be text, bytes or None
        return None, None
    return body.get('errorCode'), body.get('message')


class Client(capito.Client):         # the project client subclasses the capito client under the same name
    core = Core(auth=MyAuth,                  # capito mechanisms; on the instance, client.core
                secrets=Secrets.http().extend(keys=('code',)),
                error_parser=parse_error)
    commands = {'accounts': AccountsCommands, # command sets; on the instance, client.accounts, client.auth…
                'auth': AuthCommands,
                'items': ItemsCommands}
```

The client declares two things, and both follow one rule: the class holds the declaration, the instance holds the ready object.

| Declaration | On the instance | What it is |
| --- | --- | --- |
| `core = Core(auth=..., secrets=..., error_parser=...)` | `client.core` | capito mechanisms: authorization adapter (`client.core.auth`), configuration, transport, secrets policy, session storage. All `Core` fields are optional: by default no authorization, `Secrets.http()` and no error format |
| `commands = {name: <Name>Commands}` | `client.<name>` | The project's command sets |

Mechanisms live in `client.core` rather than directly on `client` so they do not clash with sets: an `auth` set with sign-in commands and the `client.core.auth` adapter live side by side, and new capito mechanisms never take a set's name. The top level of the client holds only the sets and `call()`, `operations()`, `describe()`, `close()`, `print(client)`; these names and `core`, `commands` are taken for sets.

`capito.Client` creates the mechanisms and all sets and registers the commands. A client has exactly the sets listed in `commands`: none by default. A set name is an identifier that does not match a top-level attribute.

**The `accounts` set is special:** the client finds the account for `as_user` through it. Under the `accounts` key only `capito.AccountsCommands` or a subclass is allowed, and conversely, a subclass under another key is an error. Without an `accounts` set the client has no `client.accounts`, and `as_user` raises a clear error; anonymous requests, help and validation work. An API with authorization needs at least the base `'accounts': capito.AccountsCommands`.

Declaration errors show up at once: wrong `Core` fields when the class is defined; a missing token path, an unimplemented abstract adapter method or a wrong set when the client is created, not on the first request.

## Modules

| Module | Responsible for |
| --- | --- |
| `client.py` | `Client`: project declaration, command sets, `call()`, help, `print(client)` |
| `core.py` | `Core`, the declaration of mechanisms; `client.core`: configuration, transport, adapter, session storage, help registry |
| `config.py` | `capito.toml`: lookup, environments, contracts, state, `[view]` limits |
| `transport.py` | URL building, body encoding, sending through httpx |
| `files.py` | `File` for multipart and `readable_file()`, which checks that a file exists and is readable |
| `contracts.py` | Reading OpenAPI from a file or an address, the `api:` catalog, examples, compact schema notation |
| `validation.py` | Checking requests and responses against contracts with openapi-core |
| `auth.py` | `Auth` and `BearerAuth` |
| `accounts.py` | Accounts: `AccountRecord`, `Session`, record storage and environment lock, the `AccountsCommands` command set |
| `commands.py` | `Commands`, the base of command sets |
| `results.py` | `Result`, `CompoundResult`, `Account`, the `Secrets` policy |
| `view.py` | Shared `view()` rendering and limits |
| `registry.py` | `@command`, `operations()`, `describe()`, the `Help` card, parameter description helpers |
| `testing.py` | Helpers for project tests |
| `__main__.py` | CLI for help and documentation; sends no API requests |

## Commands

A command is a method of a set (a `Commands` subclass) with the `@command` decorator. The operation string is written explicitly: in `api=`, in the description helpers and in the call.

```python
from capito import Commands, command, path_param


class ItemsCommands(Commands):
    @command('Get item',
             api=('api:GET /items/{itemId}',),
             parameters={'item_id': path_param('api:GET /items/{itemId}', 'itemId', 'Item ID')})
    def get(self, item_id, *, as_user=None, validate=None):
        return self.client.call('GET', '/items/{itemId}', path={'itemId': item_id}, as_user=as_user, validate=validate)
```

- **Strict signature:** domain arguments, `as_user=None` and `validate=None`, and `query` only if the operation has query parameters. Headers the operation needs, for example `Idempotency-Key`, become explicit domain arguments. The registry does not accept `**kwargs` and `*args`: headers, body, a session snapshot and `auto_refresh` are available only through `call()`.
- **Metadata only for what is specific.** Write `effects` only if the command does more than "one server request, local state unchanged"; `prerequisites` and argument descriptions only when specific to the command. Shared things are described once in `docs usage`.
- **Compound command:** `result='compound'`, `steps`, `data`, `prerequisites`; the result is `self.client.core.compound(name)`, steps are `add_step()`, failure is `failed()`, a response field is `body_value()`. A failure stops dependent steps; there is no rollback.
- **Local failure without a request:** `self.client.core.failure('reason')`, a result with the client's limits and policy.
- **Local checks** are limited to technical preparation: the file is readable, a step's response contains the needed ID. The server checks business rules.

Arguments of `@command(summary, ...)`:

| Argument | Meaning |
| --- | --- |
| `api` | Contract operations the command calls: `('api:GET /items/{itemId}',)`. For a single command they give the response body in help and the `→ command:…` mark in the catalog |
| `parameters` | Argument descriptions: `path_param(api, name, text)`, `body_param(api, text)`, `query_param(api)` take the schema from the contract; custom ones are `{'type', 'description', 'example', 'include_in_example'}`; `account_arguments(...)` for account arguments |
| `result` | What it returns: `http` (default, `Result`), `compound`, `account`, `accounts`, `session`, `text`, `none` |
| `effects`, `prerequisites`, `notes` | Consequences, what to prepare, notes; only what is specific to the command |
| `steps`, `data` | For `compound`: steps as `'name: what it does'` and `data` fields with descriptions |
| `example` | A ready call example, if the generated one does not fit |

Help is built from metadata, the signature, the docstring and the help contract's schemas; method bodies are not executed. Any endpoint can be called through `call()` without a command, and updating the contract adds it to the `api:` catalog, help and validation.

## Authorization

```
Auth                  the interface the client calls; on its own, "no authorization"
└── BearerAuth        Bearer + refresh: everything except what depends on the API
    └── MyAuth        project: two token paths and the refresh request
```

```python
from capito import BearerAuth


class MyAuth(BearerAuth):
    access_token_path = 'accessToken'         # path in the sign-in and refresh response body; nested: 'data.tokens.access'
    refresh_token_path = 'refreshToken'

    def send_refresh(self, client, refresh_token, account, *, validate):
        return client.call('POST', '/auth/refresh', body={'refreshToken': refresh_token}, validate=validate)
```

`BearerAuth` attaches `Authorization: Bearer …`, respects authorization passed explicitly in `call(headers=...)`, refreshes the session once after a 401 and repeats the request, extracts tokens from the response and saves them with a revision check. It stores the session in its own format, so project commands do not read `account.session` directly: get tokens through `client.core.auth.tokens(account)`, for example for a logout body. If the API reports an expired token other than with a 401, override `should_refresh(request, response)`.

`Auth` methods, for your own scheme if Bearer does not fit:

| Method | Who calls it and when | May | Must not |
| --- | --- | --- | --- |
| `has_credentials(request)` | `call()` before sending | read the request | change anything |
| `prepare(request, account)` | `call()` before each attempt | change `request.headers` | network, state |
| `should_refresh(request, response)` | `call()` after the first attempt, only with `as_user` | read the response | network, state |
| `refresh(client, account, *, validate)` | `client.core.refresh_session()` under the environment lock | `client.call()` without `as_user`, `client.core.save_session()` | `call(as_user=…)`: the client raises `RuntimeError` |
| `session_from(result)` | `client.core.save_session()` after a successful response | read the result | — |

The adapter is stateless: one instance per client, called from several threads.

**Sign-in** is project commands (methods of `AuthCommands` in `commands/auth.py`): capito does not know how to sign in (OTP, password…). It stores and refreshes the tokens obtained:

```python
def login(self, email, password, *, as_user, validate=None):
    account = self.client.accounts.resolve(as_user)
    result = self.client.call('POST', '/auth/login', body={'email': email, 'password': password}, validate=validate)
    return self.client.core.save_session(result, account)


def logout(self, *, as_user, validate=None):
    account = self.client.accounts.resolve(as_user)
    tokens = self.client.core.auth.tokens(account)         # None if there is no session
    result = self.client.call('POST', '/auth/logout', as_user=as_user, auto_refresh=False, validate=validate,
                              body={'refreshToken': tokens.refresh if tokens else None})
    return self.client.core.clear_session(result)
```

## Accounts

```python
import uuid

import capito
from capito import account_arguments, command


class AccountsCommands(capito.AccountsCommands):  # the project's client.accounts set
    lookup = ('email',)                       # as_user="bob@example.test" finds the account too; values are unique
    summary_identity = ('email', 'server_id') # what print(account) shows

    @command('Create a local account without contacting the server', result='account',
             effects='Creates only a local record.',
             parameters=account_arguments('alias', email={'type': 'str', 'description': 'Test user email'}))
    def create(self, alias, *, email):
        return super().create(alias, identity={'email': email, 'device': str(uuid.uuid4())})
```

- **Record:** `AccountRecord` with `id`, `alias`, `identity` (who it is; no secrets, since it shows in `print(account)`, though masked by the policy), `session` (credentials in the adapter's format), `settings` (local settings), `revision` (session revision). It is an immutable snapshot: dictionaries inside are read-only.
- **Lookup.** `client.accounts.resolve(selector)` finds by ID, alias (case-insensitive) or a `lookup` field; there is no need to override it.
- **Creation.** A custom `create` with domain arguments must delegate to `super().create(...)`: that is where alias and `lookup` field uniqueness is checked under the lock.
- **Local settings** without a server request: `self.update_local(selector, identity=..., settings=...)`. It is not a command, so a script cannot change a lookup field bypassing the project's checks.

## Mechanisms for commands: `client.core`

| Method | What it does |
| --- | --- |
| `client.core.save_session(result, account=None, *, identity=None)` | After a successful sign-in response, saves the session from `session_from()` and, if needed, `identity` fields |
| `client.core.clear_session(result, account=None, *, identity=None)` | After a successful logout or profile deletion, clears the session |
| `client.core.update_account(result, account=None, *, identity=None, settings=None)` | After a successful response, updates the record, for example `server_id` from the profile |
| `client.core.refresh_session(account, *, validate=None)` | Refreshes the session; a concurrent refresh is reused |
| `client.core.failure(message)`, `client.core.compound(name)` | Results with the client's secrets policy and limits |
| `client.accounts.resolve(selector)` | A record with credentials: for commands only, not for output |
| `client.core.auth.tokens(account)` | The record's tokens for `BearerAuth` (`access`, `refresh`), or `None` without a session |
| `client.core.contract` | The help contract (`help_contract`), read on first access; for tests and checks |
| `client.core.config`, `client.core.transport`, `client.core.secrets` | Environment configuration (for example `config.root` for paths), transport, secrets policy |
| `client.core.contracts()`, `client.core.registry` | Contract provenance; the command and help registry, for tests |

The common contract of the first three:

- on an unsuccessful `result` nothing changes and the same `result` is returned;
- on a successful one the change is applied under the environment lock; if the session changed meanwhile (the revision does not match) or the record was deleted, `result` becomes unsuccessful with a clear error, and the newer state stays;
- `save_session` without tokens in a successful response also marks `result` with an error; tokens obtained are hidden in `view()`;
- the same `result` object is returned, so a command writes `return self.client.core.save_session(...)`;
- `account` defaults to `result.sent_account`, the record the `as_user` request was sent with (taking refresh into account). For a request without `as_user`, such as sign-in, pass the record obtained before the request. If there is neither, `ValueError`.

The project does not access underscore attributes; record storage is internal.

## Secrets

`Secrets(keys, values)` is declared in `Core(secrets=...)` of the project class, so a script cannot weaken the policy; `extend()` only adds rules. A regular expression is always `re.compile(...)`: a string is never read as one.

| Rule | What it hides | When to use |
| --- | --- | --- |
| `keys`: a string | the whole value of a field, header or query parameter with exactly this key | the key may also appear in a harmless value: `code`, `session` |
| `keys`: `re.compile(...)` | the whole value of every key the expression is found in | a family of keys: `re.compile('token$')` hides `access_token`, `refreshToken` and `X-Auth-Token`, not `prompt_tokens` or `tokenizer` |
| `values`: `re.compile(...)` | the matching part of any text | a secret without a key inside text: an API key format such as `re.compile(r'itm_[A-Za-z0-9]{32}')` |
| `result.add_secrets(value)` | a specific value everywhere in the result's output, as a whole token: not inside a longer word or number | the command got a secret and knows its value: a sign-in token, a one-time code |

Key rules see the key in lower case without `-` and `_`: `X-API-Key` is `xapikey` for both a string and an expression, so write them in lower case.

`Secrets.http()` is the core preset: exact keys of authorization headers, cookies, sessions, CSRF, `credentials` and `tokens` (after Sentry's `DEFAULT_DENYLIST`), keys ending in `token`, `apikey` and `privatekey` (after GitLab's log filter: `x-goog-api-key` and `x-rapidapi-key` too), keys containing `password`, `passwd` and `secret`, keys ending in `signature` and `credential` and the exact key `sig` (what makes a signed link of S3, GCS, Azure or CloudFront work: `X-Amz-Signature`, `X-Goog-Credential`, `sig`, `Signature`), and the values `Bearer …` and JWT. A key with the word in its middle, such as `token_value`, is not hidden; a project that has one adds it. The value of every request header the policy hides is also hidden everywhere in that result: a server that repeats a key in its error message does not reveal it. Query parameters in addresses (`?api_key=…`, the signature of a signed link) are hidden by the same key rules; the parameter name and the rest of the link stay visible. So are `key=value` pairs of a form in text, from the first one of a line (`client_secret=…&grant_type=…`), and `"key": value` pairs inside text, such as a JSON response that did not parse or was cut off: the whole value is hidden, as in a parsed body. Other text formats (`key: value`, XML) are not recognized. A generic key such as `key` is not in the preset, since many APIs use it for plain values; an API that passes its key as `?key=…` adds it: `Secrets.http().extend(keys=('key',))`. `print(client.core.secrets)` shows the effective rules. The policy does not hide personal data such as phone numbers and names.

## Request execution

`call()` step by step: checks the arguments; builds the URL from the template, `path` and `query`; encodes the body once (`body`, `multipart` or `raw`); the adapter attaches credentials if `as_user` or `session` was passed; the transport sends the request; after a matching response with `as_user`, one refresh and one repeat; the response is parsed and, if enabled, checked against all contracts.

The transport is httpx with a connection pool per client: no proxies, `.netrc` or certificates from the environment, no redirects, no retries. Bodies and credentials are not written to disk. `interface` binds the socket to an interface by name (`IP_BOUND_IF` on macOS, `SO_BINDTODEVICE` on Linux; IPv4 connections only); an unknown interface gives an unsuccessful result with `status=0`.

## Accounts on disk and locking

State lives in `<state>/<environment>` and is bound to `base_url`. It is file storage without encryption: on macOS and Linux directories have mode `0700` and files `0600`; on Windows permissions come from the user profile (Windows support has not been tested). There is one `.lock` per environment: the thread lock is reentrant, and `flock` (`msvcrt.locking` on Windows) coordinates processes; the wait limit is 30 seconds. Before a change the record is re-read under the lock and replaced atomically. Refresh holds the lock during the network request too; ordinary requests run outside the lock and in parallel.

## Project tests

Tests check what the project decides: its commands' requests, the logic of compound commands, its own local rules and the `Core` declaration. capito mechanisms (refresh, session races, storage, help, CLI) are tested by capito; there is no need to repeat them.

**The layout mirrors the sources:** `my_api/tests/test_<module>.py` for modules such as `auth.py` and `core.py`, `my_api/tests/commands/test_<set>.py` for `commands/<set>.py`. On a large API a test file grows together with its module.

**Network: respx**, the standard mock for httpx. A route sets the response, and calls are recorded:

```python
def test_get(alice, respx_mock):
    route = respx_mock.get(f'{BASE_URL}/items/{ID}').respond(200, json={'id': ID})
    result = alice.items.get(ID, as_user='alice')
    assert route.called and valid_request(result)
```

respx is strict: a test fails on a request without a route and on a route that was not called. So a test "a compound command stops when a step fails" sets routes only for the steps that should happen, and that is how it proves no further requests were sent. Responses in sequence: `route.side_effect = [httpx.Response(...), ...]`; a response computed from the request: a function in `side_effect`.

**Contract conformance through ordinary validation.** The test client is created with `validate=True`, and a command test checks `result.validation['<contract>']['request']['status'] == 'valid'`.

`capito.testing` (extra `test`: pytest and respx):

| Helper | Purpose |
| --- | --- |
| `no_network` | Autouse fixture: import it in `conftest.py`, and every test runs under `respx_mock` |
| `config(tmp_path, contract, analyst=None, *, base_url='http://api.test', view=...)` | Configuration in a temporary directory; contracts `backend_api` and `analyst_api`; limits are `all` unless `view` is passed (for example the project's `Config.load(...).view`) |
| `put_session(client, selector, session, *, identity=None)` | Setup: save a session in the adapter's format (`BearerAuth`: `{'access_token', 'refresh_token'}`) |

```python
# my_api/tests/__init__.py
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / 'my-api.yaml'
BASE_URL = 'http://api.test'

# my_api/tests/conftest.py
import pytest
from capito import Config
from capito.testing import config, no_network, put_session  # noqa: F401  (no_network: autouse fixture)
from my_api import Client
from my_api.tests import CONTRACT, ROOT

VIEW = Config.load(ROOT / 'capito.toml').view


@pytest.fixture
def client(tmp_path):
    return Client(config(tmp_path, CONTRACT, view=VIEW), validate=True)
```

One test checks the project's help: help builds for every command. capito reports declaration errors itself: when the client is created (an undescribed argument, a typo in `parameters`, an `api=` or `schema_from` reference not of the form `api:GET /items/{itemId}`, a compound command without `steps`/`data`) and when help is first built (an `api=` or `schema_from` reference to something missing from the contract):

```python
def test_help_builds_for_every_command_and_operation(client):
    for operation in client.operations():
        client.describe(operation.id).view(depth='all')
```
