"""capito.toml: lookup, contracts, state, view limits and strictness."""
import pytest

from capito.config import Config, find_config
from capito.view import Limits
from helpers import schema


BASE = 'client = "my_api:Client"\ndefault_environment = "dev"\n[environments.dev]\nbase_url = "http://localhost"\n'


def write(tmp_path, text, name='capito.toml'):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_contract_paths_are_relative_to_the_file_and_urls_stay_urls(tmp_path, monkeypatch):
    backend = schema(tmp_path)
    path = write(tmp_path, BASE + 'help_contract = "backend"\n[environments.dev.contracts]\n'
                 'backend = "api.json"\nreference = "https://docs.example.test/openapi.yaml"\nhome = "~/spec.yaml"\n')
    monkeypatch.chdir('/')
    config = Config.load(path)
    assert config.contracts['backend'] == backend
    assert config.contracts['reference'] == 'https://docs.example.test/openapi.yaml'
    assert str(config.contracts['home']).startswith(str(tmp_path.home()))
    assert config.help_contract == 'backend' and config.client == 'my_api:Client'
    assert config.state == tmp_path / 'capito_state' / 'dev'
    with pytest.raises(ValueError):
        Config.load(path, 'missing')


def test_config_is_found_upwards_from_the_working_directory(tmp_path, monkeypatch):
    path = write(tmp_path, BASE + '[environments.dev.contracts]\nmain = "api.json"\n')
    nested = tmp_path / 'qa' / 'reports'
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert find_config() == path and Config.load().path == path
    monkeypatch.chdir('/')
    with pytest.raises(FileNotFoundError, match='capito.toml not found'):
        find_config()


def test_help_contract_is_needed_only_when_ambiguous(tmp_path):
    single = Config.load(write(tmp_path, BASE + '[environments.dev.contracts]\nmain = "api.json"\n'))
    assert single.help_contract == 'main'
    with pytest.raises(ValueError, match='Choose help_contract'):
        Config.load(write(tmp_path, BASE + '[environments.dev.contracts]\na = "a.json"\nb = "b.json"\n'))
    with pytest.raises(ValueError, match='not in contracts'):
        Config.load(write(tmp_path, BASE + 'help_contract = "c"\n[environments.dev.contracts]\na = "a.json"\n'))


@pytest.mark.parametrize('extra', ['[documentation]\nrequirements = "../docs"\n', 'colour = "red"\n'])
def test_unknown_keys_are_errors(tmp_path, extra):
    with pytest.raises(ValueError, match='Unknown keys'):
        Config.load(write(tmp_path, extra + BASE + '[environments.dev.contracts]\nmain = "api.json"\n'))


@pytest.mark.parametrize('source', ['file:///tmp/api.yaml', 'ftp://example.test/api.yaml'])
def test_contract_sources_are_paths_or_http_urls(tmp_path, source):
    with pytest.raises(ValueError, match='Unsupported contract source'):
        Config.load(write(tmp_path, BASE + f'[environments.dev.contracts]\nmain = "{source}"\n'))


@pytest.mark.parametrize('url', ['ftp://localhost', 'https://user:pass@example.test', 'https://:pass@example.test', 'https://example.test?q=1', 'https://example.test#fragment'])
def test_reject_invalid_server_origin(tmp_path, url):
    text = f'default_environment="dev"\n[environments.dev]\nbase_url="{url}"\n[environments.dev.contracts]\nmain="api.json"\n'
    with pytest.raises(ValueError):
        Config.load(write(tmp_path, text))


def test_environment_override_state_and_network_options(tmp_path):
    text = (BASE + '[environments.dev.contracts]\nmain = "shared.yaml"\n'
            '[environments.local]\nbase_url = "http://127.0.0.1:8080/"\ntimeout = 2\nconnect_timeout = 1\ninterface = "lo0"\n'
            '[environments.local.contracts]\nmain = "shared.yaml"\n')
    path = write(tmp_path, 'state = "../shared-state"\n' + text)
    dev, local = Config.load(path), Config.load(path, 'local')
    assert local.contracts == dev.contracts and local.base_url == 'http://127.0.0.1:8080'
    assert dev.state == (tmp_path / '../shared-state/dev').resolve() and local.state != dev.state
    assert local.timeout == 2 and local.connect_timeout == 1 and local.interface == 'lo0'


@pytest.mark.parametrize('extra', ['timeout=0', 'connect_timeout=-1'])
def test_nonpositive_timeouts_rejected(tmp_path, extra):
    with pytest.raises(ValueError):
        Config.load(write(tmp_path, BASE + extra + '\n[environments.dev.contracts]\nmain = "api.json"\n'))


def test_view_subsections_inherit_from_view_and_default_to_all(tmp_path):
    text = (BASE + '[environments.dev.contracts]\nmain = "api.json"\n'
            '[view]\ndepth = 1\nitems = 5\nwidth = 80\nlines = 60\n[view.help]\nwidth = 200\nitems = "all"\nlines = "all"\n'
            '[view.list]\ndepth = 0\n')
    view = Config.load(write(tmp_path, text)).view
    assert view['result'] == view['account'] == Limits(1, 5, 80, 60)
    assert view['help'] == Limits(1, None, 200, None)
    assert view['list'] == Limits(0, 5, 80, 60)
    bare = Config.load(write(tmp_path, BASE + '[environments.dev.contracts]\nmain = "api.json"\n')).view
    assert all(limits == Limits() for limits in bare.values())


@pytest.mark.parametrize('extra,message', [
    ('[view.helps]\nwidth = 1\n', 'Unknown \\[view\\] keys'),
    ('[view]\ncolumns = 1\n', 'Unknown \\[view\\] keys'),
    ('[view.help]\ncolumns = 1\n', 'Unknown \\[view.help\\] keys'),
    ('[view]\ndepth = -1\n', 'non-negative'),
    ('[view]\ndepth = "many"\n', 'non-negative'),
])
def test_view_typos_and_bad_values_are_errors(tmp_path, extra, message):
    with pytest.raises(ValueError, match=message):
        Config.load(write(tmp_path, BASE + '[environments.dev.contracts]\nmain = "api.json"\n' + extra))


CONTRACTS = '[environments.dev.contracts]\nmain = "api.json"\n'


@pytest.mark.parametrize('text, message', [
    ('default_environment = 1\n[environments.dev]\nbase_url = "http://localhost"\n' + CONTRACTS,
     'default_environment must be text, got 1'),
    (BASE + 'timeout = true\n' + CONTRACTS, r'\[environments.dev\] timeout must be a number of seconds, got true'),
    (BASE + 'connect_timeout = "10"\n' + CONTRACTS, r'connect_timeout must be a number of seconds, got "10"'),
    (BASE + 'interface = 5\n' + CONTRACTS, r'\[environments.dev\] interface must be text, got 5'),
    (BASE + 'help_contract = ["main"]\n' + CONTRACTS, r'help_contract must be text, got \["main"\]'),
    ('default_environment = "dev"\n[environments.dev]\nbase_url = 8080\n' + CONTRACTS, 'base_url must be text, got 8080'),
    ('default_environment = "dev"\nenvironments = "dev"\n', r'\[environments\] must be a table'),
    ('default_environment = "dev"\n[environments]\ndev = "http://localhost"\n', r'\[environments.dev\] must be a table'),
    ('view = 1\n' + BASE + CONTRACTS, r'\[view\] must be a table, got 1'),
])
def test_a_value_of_the_wrong_type_is_an_error_naming_its_key(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        Config.load(write(tmp_path, text))
