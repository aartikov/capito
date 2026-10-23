"""python -m capito: the client named in capito.toml, configuration found upwards, packaged docs."""
import subprocess
import sys
from importlib import resources

import pytest

from capito.__main__ import load_client, main


def test_cli_loads_the_declared_client_from_a_nested_directory(project, monkeypatch, capsys):
    root, _ = project
    (root / 'qa').mkdir()
    monkeypatch.chdir(root / 'qa')
    assert main(['operations', '--kind', 'api']) == 0
    assert 'api:GET /things' in capsys.readouterr().out
    assert type(load_client()).__name__ == 'Client'


@pytest.mark.parametrize('client,message', [('tiny_api:NotAClient', 'not a subclass of capito.Client'),
                                            ('tiny_api:Missing', 'cannot be imported'), ('no_such_module:Client', 'cannot be imported')])
def test_a_wrong_client_key_is_exit_code_2(project, monkeypatch, capsys, client, message):
    root, write = project
    write(client)
    monkeypatch.chdir(root)
    assert main(['operations']) == 2
    assert message in capsys.readouterr().err


def test_docs_need_no_configuration(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(['docs']) == 0 and 'usage' in capsys.readouterr().out
    for topic in ('usage', 'extending'):  # the command prints the packaged file as it is
        packaged = resources.files('capito').joinpath('docs', f'{topic}.md').read_text(encoding='utf-8')
        assert main(['docs', topic]) == 0 and capsys.readouterr().out == packaged + '\n'


def test_module_runs_as_a_program(tmp_path):
    done = subprocess.run([sys.executable, '-m', 'capito', 'docs'], capture_output=True, text=True, cwd=tmp_path)
    assert done.returncode == 0 and 'extending' in done.stdout
