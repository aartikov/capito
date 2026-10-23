# capito

A ready tool for working with HTTP APIs from Python scripts and agents, extended by each project with its own commands: help from the OpenAPI contract, calls, accounts with Bearer sessions, contract checks, result viewing with secret masking. Not a testing framework: it knows nothing about plans, reports or requirements.

## Installation

Python 3.12+:

```
pip install capito
```

In a project, pin the version, for example `capito==0.1.1` in `requirements.lock`.

## Documentation

Served by the installed version: `python -m capito docs usage` for working with an API, `python -m capito docs extending` for connecting your own API.

Development goes through an agent; the rules are in [AGENTS.md](https://github.com/aartikov/capito/blob/main/AGENTS.md).
