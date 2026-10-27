"""capito.toml: environments, contracts, local state and view limits. Paths resolve from the file, never the cwd."""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
import tomllib
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from .view import ALL, KINDS, LIMIT_NAMES, Limits, limit_value

CONFIG_NAME = 'capito.toml'
DEFAULT_STATE = 'capito_state'
TOP_KEYS = {'client', 'default_environment', 'state', 'environments', 'view'}
ENVIRONMENT_KEYS = {'base_url', 'interface', 'timeout', 'connect_timeout', 'help_contract', 'contracts'}


def find_config(start=None):
    """capito.toml in start (default: the working directory) or the nearest parent directory."""
    start = Path(start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f'{CONFIG_NAME} not found in {start} or its parents; pass the path explicitly')


def contract_source(value, root):
    """An http(s) URL as is, or a local path: ~ expands, a relative path is relative to root."""
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f'Contract source must be a path or an http(s) URL, got {value!r}')
    text = str(value)
    scheme = urlsplit(text).scheme.lower()
    if scheme in ('http', 'https'):
        return text
    if re.fullmatch(r'[a-z][a-z0-9+.-]+', scheme):  # file:, ftp:… (a one-letter scheme would be a Windows drive)
        raise ValueError(f'Unsupported contract source {text!r}: use a local path or an http(s) URL')
    path = Path(text).expanduser()
    return (Path(root) / path).resolve() if not path.is_absolute() else path


def shown(value):
    """A value as the TOML file has it: true, 5, "x"."""
    return json.dumps(value, default=str, ensure_ascii=False)


def text(where, key, value):
    """An optional string key of the file; a value of another type is an error naming the key."""
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f'{where} {key} must be text, got {shown(value)}')
    return value


def seconds(where, key, value, default):
    """A timeout of the file: a number of seconds, not true or "30"."""
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{where} {key} must be a number of seconds, got {shown(value)}')
    return float(value)


def table(where, value):
    if not isinstance(value, dict):
        raise ValueError(f'{where} must be a table, got {shown(value)}')
    return value


def view_limits(section):
    """[view] and its kind subsections; a kind without a value takes [view], then 'all'."""
    section = dict(section or {})
    unknown = set(section) - set(LIMIT_NAMES) - set(KINDS)
    if unknown:
        raise ValueError(f'Unknown [view] keys: {sorted(unknown)}; limits are {", ".join(LIMIT_NAMES)}, '
                         f'subsections are {", ".join(KINDS)}')
    base = {name: limit_value(name, section[name]) for name in LIMIT_NAMES if name in section}
    result = {}
    for kind in KINDS:
        own = section.get(kind, {})
        if not isinstance(own, dict):
            raise ValueError(f'[view.{kind}] must be a table')
        extra = set(own) - set(LIMIT_NAMES)
        if extra:
            raise ValueError(f'Unknown [view.{kind}] keys: {sorted(extra)}')
        values = {**base, **{name: limit_value(name, own[name]) for name in LIMIT_NAMES if name in own}}
        result[kind] = Limits(**values)
    return MappingProxyType(result)


def default_view():
    return MappingProxyType({kind: ALL for kind in KINDS})


@dataclass(frozen=True)
class Config:
    root: Path                                   # directory of capito.toml; relative paths start here
    environment: str
    base_url: str
    contracts: Mapping[str, str | Path]          # name -> local path or URL; names label validation output
    help_contract: str | None = None             # the contract behind operations()/describe()
    client: str | None = None                    # 'package.module:Client' used by the CLI
    state_dir: Path | None = None                # local accounts; default <root>/capito_state
    interface: str | None = None
    timeout: float = 30
    connect_timeout: float = 10
    view: Mapping[str, Limits] = field(default_factory=default_view)
    path: Path | None = None                     # the file this configuration was loaded from

    def __post_init__(self):
        if not self.contracts:
            raise ValueError('At least one contract is required')
        if self.help_contract is None:
            if len(self.contracts) > 1:
                raise ValueError(f'Choose help_contract: one of {", ".join(self.contracts)}')
            object.__setattr__(self, 'help_contract', next(iter(self.contracts)))
        elif self.help_contract not in self.contracts:
            raise ValueError(f'help_contract {self.help_contract!r} is not in contracts: {", ".join(self.contracts)}')
        if self.interface and sys.platform == 'win32':
            raise ValueError('interface is not supported on Windows')
        if self.timeout <= 0 or self.connect_timeout <= 0:
            raise ValueError('Timeouts must be positive')
        object.__setattr__(self, 'contracts', MappingProxyType(dict(self.contracts)))

    @property
    def state(self):
        return Path(self.state_dir or Path(self.root) / DEFAULT_STATE) / self.environment

    @classmethod
    def load(cls, path=None, environment=None):
        """Read capito.toml: the given path, or the nearest one from the working directory upwards."""
        source = Path(path).resolve() if path is not None else find_config()
        data = tomllib.loads(source.read_text())
        unknown = set(data) - TOP_KEYS
        if unknown:
            raise ValueError(f'Unknown keys in {source.name}: {sorted(unknown)}')
        name = environment or text(source.name, 'default_environment', data.get('default_environment'))
        if not name or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Choose a valid environment in config or explicitly')
        environments = table('[environments]', data.get('environments', {}))
        if name not in environments:
            raise ValueError(f'Unknown environment: {name}')
        where = f'[environments.{name}]'
        env = table(where, environments[name])
        extra = set(env) - ENVIRONMENT_KEYS
        if extra:
            raise ValueError(f'Unknown keys in {where}: {sorted(extra)}')
        url = (text(where, 'base_url', env.get('base_url')) or '').rstrip('/')
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc or '@' in parts.netloc or parts.query or parts.fragment:
            raise ValueError('base_url must be an HTTP(S) server URL without credentials/query/fragment')
        contracts = env.get('contracts')
        if not isinstance(contracts, dict) or not contracts:
            raise ValueError(f'[environments.{name}.contracts] needs at least one contract')
        root = source.parent
        state = data.get('state', DEFAULT_STATE)
        if not isinstance(state, str) or not state:
            raise ValueError('state must be a directory path')
        client = data.get('client')
        if client is not None and (not isinstance(client, str) or not re.fullmatch(r'[\w.]+:[\w.]+', client)):
            raise ValueError("client must look like 'package.module:Client'")
        return cls(root=root, environment=name, base_url=url,
                   contracts={k: contract_source(v, root) for k, v in contracts.items()},
                   help_contract=text(where, 'help_contract', env.get('help_contract')), client=client,
                   state_dir=(root / Path(state).expanduser()).resolve(), interface=text(where, 'interface', env.get('interface')),
                   timeout=seconds(where, 'timeout', env.get('timeout'), 30),
                   connect_timeout=seconds(where, 'connect_timeout', env.get('connect_timeout'), 10),
                   view=view_limits(table('[view]', data.get('view', {}))), path=source)
