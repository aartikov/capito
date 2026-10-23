# Agent instructions: developing capito

This is where capito itself is developed. How to use it: `python -m capito docs usage`; how to connect an API: `docs extending`; sources in [capito/docs](capito/docs/). Read both documents before making changes.

## Focus

- capito is a way to work with HTTP APIs: calls, help from the contract, accounts and sessions, validation, result viewing, project commands. Not a testing framework: nothing about plans, bug reports, reports, requirements, QA project structure or test data, in code, configuration or documentation.
- The repository is public (github.com/aartikov/capito, MIT): no names, addresses, data or credentials of real projects anywhere, including git history.
- capito knows no specific projects. Code, tests and documentation use only made-up APIs (`/items`, `/things`) and accounts (`alice`, `robot`).
- Code, comments, tests and documentation are in English.

## Principles

- **`call()` is explicit.** Only arguments define a request's behavior. The contract is for help and validation, not for authorization, headers or encoding. A raw `call()` does not change local state, except refresh after a 401 with `as_user`.
- **No guard rails.** No bans on "dangerous" or paid operations in the client: the caller decides. The server checks business rules; local checks are technical preparation only.
- **Compact help.** `describe()` saves tokens: schemas in compact notation with a depth limit, shared things described once in `docs usage`, not in every card. When changing help, print sample cards and watch their size.
- **Two output planes.** Logic uses object attributes; reading uses `print(x)` and `x.view(...)`, which always mask secrets. New output objects implement `summary()`/`view_tree()` and use `view.py`. Limits are `view` parameters with defaults from the `[view]` configuration; `"all"` lifts a limit.
- **Mechanisms in `client.core`, command sets at the top level.** A new mechanism goes into `Core` and `client.core`, not directly into the client, so it never clashes with project sets. The secrets policy is declared in `Core(secrets=...)` of the project class, not as an argument or global state.
- **No implicitness.** No reading of environment variables, no guessing values by convention, no silent defaults where an error is better: a typo in the configuration or an incomplete project declaration is an error.
- **Simplicity.** Every addition is justified by friction already seen in practice; convenience helpers are for boilerplate every project would otherwise write.

## Environment

Python 3.12+. If there is no `.venv`, from this directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
```

## Compatibility and release

- The public API is what `capito/__init__.py` exports, `capito.testing`, the CLI, the `capito.toml` keys and the methods described in `docs extending`. Reflect changes to it in the documentation and the version.
- Changing the file format in `capito_state` breaks projects' sessions: avoid it or describe the migration.
- A release is a git tag `vX.Y.Z` with the same `version` in `pyproject.toml`. While the version is below 1.0, an incompatible change is a new minor version. Projects pin capito by tag and upgrade themselves by changing the tag.
- Platforms: macOS and Linux are tested; Windows is supported in code (locking through `msvcrt`, `os.replace` retry) but not tested; `interface` is not supported on Windows. `docs usage` and `docs extending` say the same; when changing support, update all three places.

## Checks

- `.venv/bin/python -m pytest -q`. Tests follow modules: `tests/test_<module>.py` for `capito/<module>.py`; shared parts are `tests/helpers.py` and the fixtures in `tests/conftest.py`. respx (`respx_mock`) replaces the network; a request without a route fails the test, and a real server is allowed only on loopback through `pass_through()`.
- Write a test for new behavior from an independently stated expectation, not by repeating the implementation.
- Do not leave one-off scripts or intermediate files in the repository.
