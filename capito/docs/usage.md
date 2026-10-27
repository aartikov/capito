# Using capito

capito is a ready tool for working with HTTP APIs from Python, extended by each project with its own commands: it keeps local accounts and sessions, sends requests, optionally checks them against OpenAPI contracts and builds help from the contract. The order of actions and the business checks belong to your Python script; scripts run with the project's `.venv/bin/python`. For help there is the CLI `python -m capito` ([section 3](#3-find-an-operation)).

This document applies to any connected API and matches the installed capito version (`python -m capito docs usage`). Examples use a **made-up `/items` API**, the client `my_api.Client` and the account `alice`: they illustrate techniques, not operations of your project. What is specific to the connected API (sign-in, error format, account model) is described in the project's documentation. How to connect your own API: `python -m capito docs extending`.

## 1. In short

1. **Find → read the help → call → look:** `operations(...)` → `describe(id)` → a command or `call()` → `print(r)` / `r.view(...)`.
2. **Two planes.** Logic works with attributes (`r.status`, `r.body`, `r.data`…): these are raw data. Look at a result only through `x.view(...)`, which returns a ready string, and `print(x)`, which prints `x.view()` with the limits from the config; both always mask secrets. Do not print raw `r.body`, `r.request`, `r.headers`.
3. **The account goes into every call:** `as_user="alice"`. Without it the request is anonymous; there is no "current user". Accounts exist if the project declared an `accounts` command set; otherwise `as_user` raises an error.
4. **A domain command beats `call()`** when its behavior fits. `call()` is for an endpoint without a command and for exact control of the request.
5. **Mutating calls only within the task.** Help and examples do not grant permission to create anything.
6. **A compound command does not roll back.** When a step fails, what was done stays done; the IDs obtained are in `data`.
7. **No retries.** Find out what happened to a mutating request after a timeout or 5xx before sending it again.

## 2. Quick start

```python
from my_api import Client                                     # the project's class; its name is the client key in capito.toml

client = Client.from_config(validate=True)                    # nearest capito.toml upwards from the working directory
print(client)                                                 # class, environment, address, contracts, view limits
print(client.operations("items"))                             # what exists about items
print(client.describe("command:items.get"))                   # signature, effects, response shape
r = client.items.get("item-1", as_user="alice")
print(r)                                                      # one line: status, error, validation, body shape
print(r.view("body"))                                         # body fields down to the [view] level of the config
if r.success:
    owner_id = r.body["owner"]["id"]                          # logic works on raw data
```

`from_config(path=None, *, environment=None, validate=False)`:

- **File.** An explicit `path`, or `capito.toml` in the working directory or the nearest parent, the way `git` finds a repository. If there is no file, the error names the directory the search started from. Paths inside the file are relative to its directory.
- **Class.** The file's `client` key must lead to the same class `from_config` was called on (any import path to it works: `capito:Client` and `capito.client:Client` are one class); otherwise `ValueError`. A file without the key is meant for the base `capito.Client`. The CLI loads the client by the same rule, so code and CLI work with the same client.
- **Environment.** Without `environment`, `default_environment` is used. capito reads no environment variables.
- **`validate=True`** turns on contract checks for all calls of the client.
- Creating a client reads no contracts and sends no requests: contracts load on the first help or check.

`print(client)` shows what was chosen: class, configuration file, environment and address, state directory, contracts (source; once loaded, SHA-256 and version) and the effective view limits.

```toml
client = "my_api:Client"            # the project's module:class
default_environment = "dev"
state = "capito_state"              # accounts and sessions; capito_state next to the file by default

[environments.dev]
base_url = "https://dev.example.test"
help_contract = "backend_api"       # the contract help is built from; not needed with a single contract
interface = "en0"                   # optional: reach the network only through this interface
timeout = 30
connect_timeout = 10

[environments.dev.contracts]        # the name is the label in validation output
backend_api = "api/backend_api.yaml"                        # path relative to the file's directory, absolute or ~/…
analyst_api = "https://docs.example.test/openapi.yaml"      # or an http(s) address

[view]                              # the project's view limits (section 6); without the section everything is "all"
depth = 1
items = 5
width = 120
lines = 60

[view.help]
width = 240
items = "all"
lines = "all"

[view.list]
depth = 0
```

| Key | Purpose |
| --- | --- |
| `client` | `module:Class` of the project's client. Without the key, the base `capito.Client` ("Without a project" below) |
| `default_environment` | Default environment |
| `state` | Directory of accounts and sessions; one subdirectory per environment inside |
| `base_url` | Server address |
| `contracts` | At least one contract (OpenAPI 3, YAML or JSON; the format is detected from the content). Validation runs against all of them; the name is the label in the output |
| `help_contract` | Contract for help and the `api:` catalog; required when there are several contracts |
| `timeout`, `connect_timeout` | Timeouts in seconds (30 and 10 by default): `connect_timeout` for connecting, `timeout` for each stage of the exchange; not a limit on the whole request |
| `interface` | Name of the network interface connections go through, for example to bypass a VPN. macOS and Linux; connections are then IPv4 only. Not supported on Windows |
| `[view]`, `[view.result\|help\|list\|account]` | Default view limits (section 6) |

An unknown key or section is a load error: a typo does not pass silently.

**A contract at an address** is downloaded on the first help or check with the same transport as requests (with `interface` and timeouts), without authorization, and kept in process memory. Every new process, including every CLI call, downloads it again. Download a spec that requires authorization to a file and give its path. External `$ref`s (to other files) are not supported: a contract is a single document.

**Without a project.** If the API has no authorization or you are only exploring it from the spec, no project code is needed: an `capito.toml` without the `client` key, with an address and a contract, is enough.

```python
import capito

client = capito.Client.from_config()
print(client.operations(kind="api"))
r = client.call("GET", "/items", query={"limit": 10})
```

The base client gives help from the contract, `call()`, validation and result viewing. It has no domain commands, no accounts and no `as_user`, and `r.error["code"]` is empty (the API's error format is not declared). An authorization header can be passed explicitly in `call(headers=...)`. Sign-in, refresh and commands are project code: `python -m capito docs extending`.

**State** lives in `<state>/<environment>` and is bound to `base_url`: if the address changes, the client refuses to work with the old state; create a separate environment instead.

## 3. Find an operation

**List.** `client.operations(text=None, *, kind=None)` lists commands and operations of the help contract (`help_contract`). `text` searches a substring in `id` and `summary`, case-insensitively; `kind` is `"command"` or `"api"`.

```python
print(client.operations("items", kind="api"))
# api:GET /items/{itemId}  Get item  → command:items.get
print(client.operations("items", kind="command").view(depth=1))
# command:items.get  Get item
#     client.items.get(item_id, *, as_user=None, validate=None)
#     effects: …                      (if the command sends more than one request)
```

`→ command:…` on an `api:` line is the wrapping command, which is preferable. For logic, list rows have the attributes `id`, `kind`, `summary`, `command`, `signature`, `effects`.

**Help.** `client.describe(id)` accepts three kinds of identifiers:

| id | Describes |
| --- | --- |
| `command:<set>.<command>` | A command: call, arguments, effects, what it returns, example |
| `api:<METHOD> <route>` | A contract operation: authorization, parameters, responses for every code, `call()` example |
| `schema:<Name>` | A named contract schema |

On a typo `describe` raises `ValueError` with the closest options: `Unknown schema: FileDTO; did you mean FileDto?`. Help reads no accounts and does not call the API; the network is needed only for the first load of a contract given by address.

`print(help)` prints `help.view()`: the card with the `[view.help]` limits; without them the card is complete, and with `depth = 1` from the example above objects are expanded one level. Deeper, a named object is shown by name and an inline one by its keys `{a, b, c, +N}`; `↓K` on both says how much to raise `depth` to reach the leaves, as in `view()`. Named and inline objects count the same way, and a schema met at different levels is resolved from the shallowest one. Deeper: `help.view(depth=2)`; everything: `help.view(depth="all")`; one schema: `describe("schema:Name")`. Card fields are attributes: `help.call`, `help.returns`, `help.example`; a missing field is `None`.

| Field | Command | `api:` |
| --- | --- | --- |
| `summary`, `description` | What it does | Description from the contract |
| `call` / `request` | Signature and a line per domain argument, body schema below | Parameters (`path`, `query`, `header`) and body with content type |
| `auth` | — | Authorization scheme from the contract, or `none` |
| `api` | Operations the command calls | — |
| `effects`, `prerequisites` | Only if the command does more than "one request, local state unchanged", and what to prepare | — |
| `returns` | For a single command, `Result` and the success body schema; for a compound one, steps and `data` fields | `Result` and body schema for each code: `none` for a response without a body (`HEAD`, 1xx, 204, 304), `not described` when the contract gives no body schema |
| `example` | A ready call | A ready `client.call(...)` |
| `commands` | — | Wrapping commands |

IDs, values, files (`upload.bin`) and the account `alice` in `example` are placeholders: help does not check that entities exist or that the account has access. Values come from the contract's examples, else they satisfy the schema's bounds; what help cannot build is marked: `<pattern …>` for a string with a pattern, `raw=b'<application/xml body>'` for a body that `call()` does not encode. Without an `accounts` set there is no `as_user`: the example of a protected operation passes the credentials its contract asks for, with placeholders such as `headers={'Authorization': 'Bearer <token>'}` or `query={'key': '<api key>'}`. `as_user` and `validate` exist on almost every command and are not spelled out in `call`.

**Compact schema notation**, one field per line:

| Notation | Meaning |
| --- | --- |
| `name: T`, `name?: T` | Required and optional field |
| `  # …` | Field description from the contract |
| `T \| null` | `null` is allowed |
| `A\|B\|C` | One of the values; `A\|B\|…+4 (items=2)` means the enum was shortened by `items` |
| `T[](<=5)` | An array and its constraints |
| `A \| B`, `A & B` | `oneOf`/`anyOf` and `allOf` |
| `integer(minimum=1, default=20)` | Scalar format or type and its constraints |
| `Name`, `Name ↓K` | A named object; `Name = {…}` is its definition at the end of the block. No definition means the depth ran out; `↓K` says how much to add (no marker means +1 is enough) |
| `{a, b, c, +N} ↓K` | An inline object beyond the depth limit: its first keys and how many levels lie below it (`↓` from 2) |
| `dict[string, T]` | An object with arbitrary keys |
| `object(closed)` | A closed object without fields: any field sent is a contract violation |

The full schema with all keywords is in the contract file; validation uses exactly that.

**From the shell.** `python -m capito` prints the same as `print(...view(...))`, without Python boilerplate. Handy while searching: several cards in one call. The CLI sends no API requests; it only gives help and documentation.

```bash
.venv/bin/python -m capito operations items --kind command --depth 1
.venv/bin/python -m capito describe command:items.get "api:GET /items/{itemId}" --fields call returns
.venv/bin/python -m capito describe schema:Item --depth all
.venv/bin/python -m capito docs usage
```

| Option | Equivalent |
| --- | --- |
| `operations [text] --kind command\|api` | `client.operations(text, kind=)` |
| `describe id [id …]` | `client.describe(id)` for each; cards are separated by a blank line |
| `--fields f …` | `help.view("f", …)` |
| `--depth`, `--items`, `--width`, `--lines` | The same `view` parameters; without the option, from the config; `all` means no limit |
| `--json` | `format="json"`: the complete card, `--depth` and `--items` do not apply; with several ids, an array |
| `--config`, `--env` | Configuration file and environment; by default the nearest `capito.toml` upwards from the working directory and `default_environment`. The client comes from the `client` key; without it, the base `capito.Client` |
| `docs [usage\|extending]` | Documentation of the installed version; no config needed |

Unknown id: a hint on stderr, the other cards are printed, exit code 1. A configuration, client loading or help contract error: exit code 2. Quote an `id` with a space (`api:GET /…`).

## 4. Call

**A command** is a method of a command set: `client.items.get(...)` is the command `get` of the set `items`. The signature is strict: only domain arguments, `as_user` and `validate`; an extra argument is a `TypeError`. Headers an operation needs (`Idempotency-Key`) are explicit command arguments, for example `idempotency_key`.

**`client.call(method, route, ...)`** sends exactly what its arguments describe and does not depend on the contract: the route may be missing from the contract, and validation then returns `not_described`.

```python
r = client.call("GET", "/items/{itemId}", path={"itemId": "item-1"}, as_user="alice")
page = client.call("GET", "/items", query={"limit": 10, "tag": ["a", "b"]}, as_user="alice")   # a list repeats the key
created = client.call("POST", "/items", body={"name": "Sample item"}, as_user="alice")
```

| Argument | Purpose |
| --- | --- |
| `as_user` | Local ID, alias or the value of the project's lookup field (for example a phone number); anonymous without it. Requires an `accounts` set |
| `path`, `query` | Route template and query string: scalars and lists |
| `body` | The body, encoded by its type: JSON without a type and for JSON types (`application/json; charset=utf-8`, `application/problem+json`); a dict as a form for `application/x-www-form-urlencoded`; a `str` as UTF-8 text for any other type. Anything else is a `TypeError`: encode it yourself and pass `raw` |
| `multipart` | A `multipart/form-data` form: strings, numbers, bool and `File(...)`; a list repeats the part |
| `raw` | Ready body bytes, for example broken JSON; the type is set by `content_type` |
| `content_type` | Type of `body`/`raw`, sent as `Content-Type`. The same type in `headers` has the same effect; different types in both are a `ValueError` |
| `headers` | Extra headers. An explicit `Authorization` replaces the account's authorization and disables refresh |
| `validate` | `True`/`False`; `None` means as set on the client |
| `session` | A snapshot from `client.accounts.session("alice")` instead of `as_user`; it is not updated |
| `auto_refresh` | On by default; `False` gives the raw 401 without refresh |

The body is given by exactly one of `body`, `multipart`, `raw`. `call()` itself adds only `Content-Type` by the kind of body (unless set in `headers`) and the account's authorization; the transport sets the HTTP client's service headers (`User-Agent`, `Accept`, `Accept-Encoding`, `Connection`, `Content-Length`). `Idempotency-Key` for a new action is a new key (`str(uuid.uuid4())`); to repeat the same action without a duplicate, the previous one.

**Files.** `File("path/to/cover.jpg")` reads a file from disk (path relative to the working directory) and takes the name and type from the path. `File(b"...", filename="cover.jpg", content_type="image/jpeg")` sends bytes from memory, handy when type and content should not match. Nested objects are not accepted in a form.

```python
from capito import File
r = client.call("POST", "/items/{itemId}/photos", path={"itemId": "item-1"}, as_user="alice",
                multipart={"caption": "Cover", "photo": File(b"not a jpeg", filename="cover.jpg", content_type="image/jpeg")})
```

**Exception or result.** Usage errors are exceptions: an unfilled path template, an object in `query` or `multipart`, several kinds of body, an unreadable file, an unknown account, an extra command argument. HTTP failures and transport errors are in the result, with `success = False`.

## 5. Result data

**`Result`** is one HTTP call:

| Attribute | Value |
| --- | --- |
| `success` | 2xx without transport or parsing errors. Confirms neither business meaning nor contract conformance |
| `status` | HTTP code; `0` if there was no response (timeout, connection failure); `None` for a local outcome without a request |
| `body` | JSON, text, `bytes` or `None`. Save a binary response yourself: `Path(...).write_bytes(r.body)` |
| `error` | On failure `{"code": …, "message": …}`, otherwise `None`. `code` comes from the project's error format, `None` if it cannot be parsed; `message` is the server's text or the first local reason |
| `errors` | All failure reasons as strings |
| `validation` | Contract check results (section 8) or `None` if checks are off |
| `method`, `route` | Method and route template |
| `request` | The URL, headers and body sent (files as descriptions) |
| `headers` | Response headers, names in lower case; a repeated header (`set-cookie`) is a list of values |
| `duration`, `started_at`, `finished_at`, `request_id`, `attempts` | Timing, `x-request-id`, attempts including refresh |

**`CompoundResult`** is a compound command:

| Attribute | Value |
| --- | --- |
| `name` | Command name |
| `success`, `errors` | Outcome and reasons |
| `steps` | `dict[step name, Result]` in execution order: `r.steps["publish"].status`. Step names are in `describe` |
| `data` | IDs obtained before the failure: `r.data["item_id"]`. Fields are in `describe` |

When a step fails, dependent steps do not run, what was done is not rolled back, and the IDs already obtained stay in `data`; sort them out before running again.

**Local commands** of the `accounts` set (`accounts.*`, if the project declared it) return an `Account` with the attributes `id`, `alias`, `identity`, `settings`, `has_session`, a list of accounts, a `Session`, a string or `None`. `has_session` only means tokens are stored, not that they are valid.

## 6. Viewing: `print` and `view`

Every object (result, account, list, help card) has one viewing mechanism: `x.view(...)` returns a ready string; `str(x)` is `x.view()`, so `print(x)` prints it with the default settings. Secrets are always masked; this cannot be turned off.

```python
r.view(*paths, depth=…, items=…, width=…, lines=…, format="text")   # limits not passed come from the [view] config
```

**Summary line.** `r.view()` without paths, which is what `print(r)` prints, is one line; every `view` of the result opens with it:

```
200 GET /items/{itemId} · 0.21s · backend_api: valid · analyst_api: valid · body {id, title, owner, +9} ↓3
403 POST /items · 0.10s · ForbiddenException: No access · backend_api: valid · body {errorCode, error} ↓2
— POST /items · 30.00s · timeout
FAILED items.create_with_photo · publish: 409 ConflictException · backend_api: valid · data: photo_id=7f…, item_id=a1…
OK items.create_with_photo · backend_api: upload resp=invalid · analyst_api: not_described · data: …
alice  session=yes
```

`backend_api: req=valid resp=invalid` means the phases differ; `body {…} ↓K` gives the body keys and its depth, `[N]` a list, `<N bytes>` binary content. A compound command shows a summary over steps: the common status if it is the same for all checked steps, otherwise only the deviating steps (`upload resp=invalid`, `publish invalid` for both phases); details: `view("steps.upload.validation")`.

**Paths** choose what to show. They are relative to the object: `body`, `body.owner.name`, `request`, `headers`, `validation`, `errors`, `error`, `attempts`; for a compound command, `steps`, `steps.publish.body`, `data`. `[]` goes through a list, `[0]` and `[-1]` take an element.

**`depth`** is how many levels to expand under each selected node. At the limit an object collapses to `{first 3 keys, +N}`, a list of objects to `[N] {keys of the first}`. `+N` is a preview, not a limit: all keys are visible one level further. Scalars and lists of scalars are always visible. A list does not count as a level: its elements show their fields. `depth="all"` shows everything.

**`↓K`** after a collapsed node is how many levels lie below it down to the deepest leaf: `view(node_path, depth=K)`, or `depth` raised by `K` in the same call, expands it fully. No marker means only values inside; one level is enough. For a list, the deepest element counts. The exception is a nested result (a step in `steps`): it collapses to its own summary line, and its `↓K` refers to the step's `body`, not to the step itself. Look at a step by path: `view("steps.publish.body")`.

```python
print(r.view("body.owner"))            # depth from [view], 1 here
# body.owner:
#   id: u-1
#   name: Luna
#   avatar: {id, url, fileName, +3}
#   stats: {rating, reviews} ↓2
print(r.view("body.owner", depth=2))   # avatar expanded; stats.reviews still collapsed
print(r.view("body.owner", depth=3))   # down to the leaves: 1 + ↓2
print(r.view("body.photos"))           # a list: elements with their fields
# body.photos: [2]
#   - id: p-1
#     url: https://…
#   - …
```

**Scalars only means one line.** If all selected values are scalars or lists of scalars, they are appended to the summary line. This is the format for loops:

```python
print(r.view("body.status", "body.owner.name", "body.tags[].name"))
# 200 GET /items/{itemId} · … · body.status=ACTIVE body.owner.name=Luna body.tags[].name=[a, b]
```

**Missing and `null` differ:** `body.endedAt=null` means the field exists and is `null`; `body.endedAt=<missing>` means there is no field. A path through `null` also gives `<missing>`: if `photo` is `null`, then `photo.id=<missing>`; to check the `null` itself, select the parent (`body.participants[].photo` → `[null]`).

**Limits.** `items` is list elements, `width` is characters in one value, `lines` is lines in total. Every cut names its parameter. The default comes from the configuration (below), `"all"` in a call lifts the limit, and `None` is an error with a hint: that way a stray `None` in a variable does not silently disable limits.

```
… +42 (items=5)
A leisurely walk along the seafront avenue…(+120, width=120)
… 40 more lines (lines=60); narrow the paths or raise lines
```

**Default limits** are set in `capito.toml`: the `[view]` section is the common base, subsections are the differences for a kind of object:

| Subsection | Objects |
| --- | --- |
| `[view.result]` | `Result`, `CompoundResult` |
| `[view.help]` | `describe()` cards |
| `[view.list]` | `operations()`, `accounts.list()` |
| `[view.account]` | `Account` |

A value comes from the subsection; if it is not there, from `[view]`; if not there either, `all`. capito itself limits nothing: limits are a project setting, see the example `capito.toml` in section 2. Subsections are optional, an empty one equals a missing one, and an unknown subsection is an error. `print(client)` shows the effective values.

**`format="json"`** gives the selected subtrees in their original nesting, without limits and `depth`; missing fields are left out. Without paths, the whole object. This is the format for bug report snippets and for saving a report:

```python
print(r.view("body.status", "body.endedAt", format="json"))
# {"body": {"status": "IN_PROGRESS", "endedAt": null}}
```

`view` values are masked by the project's policy (fields, headers and query parameters with tokens, passwords, secrets and API keys, authorization headers and cookies, `Bearer …` and JWTs in text; the effective rules: `print(client.core.secrets)`) and by values commands marked as secret. Personal data such as phone numbers and names is not anonymized.

| Object | `x.view()`, printed by `print(x)` | More |
| --- | --- | --- |
| `Result` | summary line | `view("body")`, `view("validation")`, `view("request", depth=2)` |
| `CompoundResult` | outcome, failed step, step validation, `data` with full IDs | `view("steps")` gives a line per step; `view("steps.publish.body")` |
| `Account` | alias, fields from the project setting, `session=yes\|no` | `view("identity", "settings")` |
| list (`operations()`, `accounts.list()`) | a line per element | `view(depth=1)`; paths apply to each element |
| `describe()` card | card with the `[view.help]` limits | `view(depth=2)`, `view("call", "returns")` |

## 7. Recipes

**ID chain.** Take data from attributes, print only the step's summary:

```python
created = client.items.create({"name": "Sample item"}, as_user="alice")
print(created)
if created.success:
    item_id = created.body["id"]
    shared = client.items.share(item_id, as_user="alice")
    print(shared)
```

**One request from several users:**

```python
for user in ("alice", "bob", "carol"):
    r = client.items.get(item_id, as_user=user)
    print(user, r.view("body.access", "body.owner.id"))
```

**An unfamiliar response, step by step:** `print(r)` → `print(r.view("body"))` → `print(r.view("body.owner", depth=2))`. Going straight to `depth="all"` on a large body costs more and gets cut by `lines`.

**Checks in code, details on mismatch:**

```python
rows = []
for item_id in ("item-1", "item-2"):
    r = client.items.get(item_id, as_user="alice")
    same = r.body.get("id") == item_id if r.success and isinstance(r.body, dict) else None   # None: not checked
    rows.append({"item_id": item_id, "status": r.status, "same_id": same})
    if same is False:
        print(r.view("body.id", "request"))
```

**Negative authorization:**

```python
denied = client.items.get("item-1")                                   # anonymous
assert denied.status == 401, denied.view("body")
old = client.accounts.session("alice")                                # snapshot before the token changes
# … logout or refresh …
stale = client.call("GET", "/items/{itemId}", path={"itemId": "item-1"}, session=old)        # old token, no refresh
bad = client.call("GET", "/items/{itemId}", path={"itemId": "item-1"}, headers={"Authorization": "Bearer wrong"})
raw = client.call("GET", "/items/{itemId}", path={"itemId": "item-1"}, as_user="alice", auto_refresh=False)  # raw 401
```

Check an expected failure by `status` and `error["code"]`: `success` is `False` for it.

**Contract violation:** `print(r.view("validation"))` gives a line per contract with the path and field names (section 8). Full data is in `r.validation`.

**Bug report snippet:** `r.view("body.status", "body.endedAt", format="json")` is exact JSON without secrets; include only the relevant fields.

**Save the final report** explicitly; capito writes no logs:

```python
import json
from pathlib import Path

report = {"contracts": client.core.contracts(), "rows": rows,
          "failed": [json.loads(r.view(format="json")) for r in failures]}
Path("report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
```

`client.core.contracts()` returns sources, versions and SHA-256 of the loaded contracts (one not loaded yet has `loaded: False`, an unreadable one `unavailable` with the reason); it is not proof that the server conforms to them. The fingerprint helps tell whether the spec at an address changed between runs.

## 8. Contract validation

Turned on for the client (`validate=True` in `from_config`) or per call. Checks run against all contracts in `contracts`; the label is the contract's name in the configuration. Violations do not block the request and do not change `success`. Raw data: `r.validation[<contract name>]["request" | "response"]` with the keys `status`, `errors`, `reason`; for a compound command it is inside each step, and the summary line aggregates them per contract.

| Status | Meaning |
| --- | --- |
| `valid` / `invalid` | Passed / has violations (in `errors`) |
| `not_described` | The operation or body schema is not in the contract |
| `not_run` | There was no HTTP response |
| `unavailable` | The contract or validator is unavailable (in `reason`) |

Only `valid` counts as success. Every schema error is `{"path", "rule", "expected"}`, plus `extra: [names]` or `missing: [names]` for extra and missing fields: names only, no values. An error in a parameter or in authorization also has `in` (`query`, `path`, `header`, `cookie` or `security`) and, for a parameter, its `name`; the path in `view` starts from them. In `view`, a line per contract gives the path, then the rule or the quoted field names; a status without violations shows its reason in brackets:

```
validation:
  backend_api: response invalid: items[0] extra 'id', 'tariff', 'amount', 'currency', 'status' +4
  analyst_api: not_described (Operation absent from contract)
```

Other lines look like `owner missing 'name'`, `tags[1].name type`, `status enum`, `query.limit maximum`, `query missing 'limit'`.

A request without authorization gives `req=invalid` (`security missing 'bearer'`) for an operation that requires it; this is expected in a negative check.

## 9. Pitfalls

- **`print(r.body)` and the like print raw data**, with tokens if it is an authorization response. Only `print(r)` and `r.view(...)`.
- **Compound commands leave traces.** An uploaded file stays on the server even if the next step failed; its ID is in `data`.
- **Timeouts and 5xx are not retried.** Find out the outcome of a mutating request before sending it again.
- **One refresh.** After a 401 with `as_user` the client refreshes the session once and repeats the request once; there is no repeated sign-in.
- **Only commands change local state, plus the automatic refresh.** A raw `call()` does not store tokens from sign-in responses and does not clear them after logout: sign in, log out and delete profiles with commands. The exception is refresh after a 401 with `as_user`: the new token pair is saved to the account. To keep the session unchanged, pass `session=` or `auto_refresh=False`.
- **One client per thread.** Account state is shared and protected by a lock; requests run in parallel. Run dependent steps of one script sequentially.
- **Connections are reused.** The client has a connection pool; `client.close()` or `with Client.from_config() as client:` closes it, otherwise it closes with the process.
- **The process environment does not affect requests.** Proxies from `HTTP(S)_PROXY`, `.netrc` and certificates from environment variables are not used; redirects are not followed: a 3xx response is returned as is.
- **Do not go through the operation catalog blindly**: some operations create, delete or cost money.
