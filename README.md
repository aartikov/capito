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

Pin the version in your project, for example `capito==0.2.2`.

### Configure

Put `capito.toml` at the root of your project. capito finds it from any subfolder, the way git finds its repository.

```toml
# client = "my_api:Client"          # your Client subclass: accounts, sign-in, commands (python -m capito docs extending)
default_environment = "dev"
state = "capito_state"              # local accounts and sessions; keep it out of git

[environments.dev]
base_url = "https://dev.example.com"
timeout = 30                        # seconds per stage of the exchange
connect_timeout = 10
help_contract = "backend_api"       # the spec that help is built from

[environments.dev.contracts]        # specs to validate against: a URL or a path
backend_api = "https://dev.example.com/openapi.yaml"     # what the backend of this environment serves
analyst_api = "docs/my-api.yaml"                         # what the analysts specified

[environments.staging]
base_url = "https://staging.example.com"

[environments.staging.contracts]    # a single spec needs no help_contract
backend_api = "https://staging.example.com/openapi.yaml"

[view]                              # default output limits; the agent can override them per call
depth = 1                           # levels of nesting expanded
items = 5
width = 120
lines = 60

[view.help]                         # help cards: wider, no cuts by items or lines
width = 240
items = "all"
lines = "all"

[view.list]                         # operations(), accounts.list(): one line per entry
depth = 0
```

### Explore and call

```python
import capito

client = capito.Client.from_config(validate=True)
print(client.operations("items"))                     # what exists about items
print(client.describe("api:GET /items/{itemId}"))     # the help card; print(x) prints x.view()
r = client.call("GET", "/items/{itemId}", path={"itemId": "item-1"})
print(r)                                              # summary: status, validation, body shape
print(r.view("body", "validation"))                   # chosen parts, deeper with depth=…
```

The same help is available from the shell: `python -m capito operations items` and `python -m capito describe "api:GET /items/{itemId}"`. Accounts, sign-in and project commands come with your own `Client` subclass: see `python -m capito docs extending`.

### What the agent sees

Everything capito shows, a help card or a call result, goes through one method: `x.view(*paths, depth=…, items=…, width=…, lines=…)`. Paths choose what to show, limits say how much. A limit that is not passed comes from `[view]` in `capito.toml`, so `print(x)` prints `x.view()` with the limits from the config. The main limit is `depth`, the number of nesting levels expanded. A collapsed part shows its keys and a marker like `↓3`: three more levels lie below it, and `depth=3` on that part reaches the leaves. In the shell the limits are options: `--depth`, `--items` and so on.

`client.describe("api:GET /items/{itemId}").view()`, the help card of an operation, lists its parameters, responses and the schemas of their bodies. At `depth = 1` from the config, `owner` is collapsed; `.view(depth="all")` would expand it:

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
  status?: DRAFT|ACTIVE|ARCHIVED
  owner: {id, name, avatar} ↓3
  tags?: {name}[](<=5)
}
Error = {
  message?: string
}
example: client.call('GET', '/items/{itemId}', path={'itemId': 'example'})
```

A call result's view starts with a summary line: status, time, the check against each spec and the top-level keys of the body. `r.view()` has no paths, so the line is all it shows:

```
200 GET /items/{itemId} · 0.03s · backend_api: valid · analyst_api: req=valid resp=invalid · body {id, name, owner, +1} ↓4
```

`r.view("body")` adds the body under the line, at `depth = 1`:

```
200 GET /items/{itemId} · 0.03s · backend_api: valid · analyst_api: req=valid resp=invalid · body {id, name, owner, +1} ↓4
body:
  id: item-1
  name: Desk lamp
  owner: {id, name, avatar} ↓3
  tags: [2] {name}
```

`r.view("body.owner", depth=3)` raises the limit for this call and opens `owner` down to the leaves:

```
200 GET /items/{itemId} · 0.03s · backend_api: valid · analyst_api: req=valid resp=invalid · body {id, name, owner, +1} ↓4
body.owner:
  id: u-1
  name: Alice
  avatar:
    url: https://cdn.example.com/a.png
    size:
      width: 256
      height: 256
```

The summary line says that the response matches the backend's spec but not the analysts' one. `r.view("validation")` shows why: the backend left out `status`, which the analysts made required:

```
200 GET /items/{itemId} · 0.03s · backend_api: valid · analyst_api: req=valid resp=invalid · body {id, name, owner, +1} ↓4
validation:
  backend_api: valid
  analyst_api: response invalid: <root> missing 'status'
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
- Before changing the client in `my_api/` (new commands, authorization, accounts), read `python -m capito docs extending`.
```

## Documentation

capito serves the docs of the installed version:

- `python -m capito docs usage` covers working with an API: help, calls, accounts, validation and viewing results ([on GitHub](https://github.com/aartikov/capito/blob/main/capito/docs/usage.md)).
- `python -m capito docs extending` covers connecting your API: the `Client` class, commands, authorization and tests ([on GitHub](https://github.com/aartikov/capito/blob/main/capito/docs/extending.md)).

## Status and license

capito is at an early stage (0.x): a minor version may include breaking changes. It is MIT-licensed. Development rules are in [AGENTS.md](https://github.com/aartikov/capito/blob/main/AGENTS.md).
