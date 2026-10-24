# capito

[![PyPI](https://img.shields.io/pypi/v/capito)](https://pypi.org/project/capito/)
[![License](https://img.shields.io/pypi/l/capito)](https://github.com/aartikov/capito/blob/main/LICENSE)

**Give your coding agent a feel for any HTTP API.** capito turns an OpenAPI spec into compact help the agent can read step by step. It keeps test users signed in, wraps multi-step flows into single commands and checks every response against the spec, all from plain Python.

## Why capito

*Capito!* is Italian for "Got it!", the moment an API clicks. There is an `api` hidden in the name, too.

When an agent works with an API, most of its effort goes into context rather than calls: huge specs, auth tokens, walls of JSON. capito keeps that context small:

- **Explore, don't dump.** The agent searches operations by keyword, opens one compact card and expands a schema only where it needs to. It never has to load a large spec in one piece.
- **Users and tokens are handled.** Named local accounts keep their Bearer sessions. Sign in once, then call with `as_user="alice"`. Your auth adapter refreshes tokens after a 401, and secrets are masked in all output.
- **Your flows become one command.** Wrap a chain like upload → create → publish into a project command with its own help. One call instead of several means fewer tokens and fewer mistakes.
- **The backend is checked against the spec.** Requests and responses can be validated against one or more OpenAPI contracts, so drift between backend and docs shows up right in the result.
- **Output is made for reading.** Results print as one-line summaries with depth and size limits. The agent drills down only where it matters.

capito is a library, not a test framework. Plans, reports and rules stay in your project.

## Quick start

### Install

Requires Python 3.12+:

```
pip install capito
```

Pin the version in your project, for example `capito==0.1.5`.

### Configure

Put `capito.toml` at the root of your project, next to the spec. capito finds it from any subfolder, the way git finds its repository.

```toml
# client = "my_api:Client"          # your Client subclass: accounts, sign-in, commands (python -m capito docs extending)
default_environment = "dev"
state = "capito_state"              # local accounts and sessions; keep it out of git

[environments.dev]
base_url = "https://dev.example.com"
timeout = 30                        # seconds per stage of the exchange
connect_timeout = 10
help_contract = "backend"           # the spec that help is built from

[environments.dev.contracts]        # specs to validate against: a path or a URL
backend = "openapi.yaml"
docs = "https://docs.example.com/openapi.yaml"

[environments.staging]
base_url = "https://staging.example.com"

[environments.staging.contracts]    # a single spec needs no help_contract
backend = "openapi.yaml"

[view]                              # how much of a result is printed by default
depth = 1
items = 5
width = 80
lines = 60

[view.help]                         # help cards in full
width = 120
items = "all"
lines = "all"
```

### Explore and call

```python
import capito

client = capito.Client.from_config(validate=True)
print(client.operations("items"))                     # what exists about items
print(client.describe("api:GET /items/{itemId}"))     # one compact card
r = client.call("GET", "/items/{itemId}", path={"itemId": "item-1"})
print(r)                                              # status, validation, body shape
print(r.view("body", "validation"))                   # drill down where needed
```

The same help is available from the shell: `python -m capito operations items` and `python -m capito describe "api:GET /items/{itemId}"`. Accounts, sign-in and project commands come with your own `Client` subclass: see `python -m capito docs extending`.

### What the agent sees

A help card holds just enough schema to make the call. `↓3` marks a part that can be expanded on request:

```
api:GET /items/{itemId} — Get item
auth: none
request: path itemId: string
returns: Result
  body on 200 (The item): Item
  body on 404 (Not found): Error
Item = {
  id: string
  name: string  # Shown in the catalog
  status: DRAFT|ACTIVE|ARCHIVED
  owner: {id, name, avatar} ↓3
  tags?: {name}[](<=5)
}
Error = {
  message?: string
}
example: client.call('GET', '/items/{itemId}', path={'itemId': 'example'})
```

A result is one line. Here it also shows that the backend returns a field the spec doesn't have:

```
200 GET /items/{itemId} · 0.03s · backend: req=valid resp=invalid · body {id, name, status, +3} ↓4
validation:
  backend: response invalid: <root> extra price
```

### Tell your agent

Add a section like this to `AGENTS.md` or `CLAUDE.md`:

```markdown
## Working with the API

Requests to the API go through capito.

- Before the first request, read `python -m capito docs usage`.
- Find an operation with `python -m capito operations <text>` and read its card with `python -m capito describe <id>` before calling it.
- Prefer a project command (`command:…`) over a raw `client.call()` when its behavior fits the task.
- In Python: `from my_api import Client; client = Client.from_config()`; pass `as_user="<account>"` for authorized requests.
- Look at results only through `print(r)` and `r.view(...)`: they mask secrets. Never print raw `r.body`, `r.headers` or `r.request`.
- Send requests that change data only when the task asks for them.
```

## Documentation

capito serves the docs of the installed version:

- `python -m capito docs usage` covers working with an API: help, calls, accounts, validation and viewing results ([on GitHub](https://github.com/aartikov/capito/blob/main/capito/docs/usage.md)).
- `python -m capito docs extending` covers connecting your API: the `Client` class, commands, authorization and tests ([on GitHub](https://github.com/aartikov/capito/blob/main/capito/docs/extending.md)).

## Status and license

capito is at an early stage (0.x): a minor version may include breaking changes. It is MIT-licensed. Development rules are in [AGENTS.md](https://github.com/aartikov/capito/blob/main/AGENTS.md).
