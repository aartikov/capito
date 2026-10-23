"""capito: explore and call an HTTP API from Python. Start with client.operations() and client.describe().

Documentation of the installed version: python -m capito docs usage | extending.
"""
from .accounts import ACCOUNT_ARGUMENTS, AccountRecord, AccountsCommands, Session, account_arguments
from .auth import Auth, BearerAuth, BearerTokens
from .client import Client
from .config import Config
from .core import Core
from .files import File, readable_file
from .registry import body_param, command, path_param, query_param
from .results import Account, CompoundResult, Result, Secrets, add_step, body_value, error_from, failed, strings
from .commands import Commands

__all__ = ['ACCOUNT_ARGUMENTS', 'Account', 'AccountRecord', 'AccountsCommands', 'Auth', 'BearerAuth', 'BearerTokens',
           'Client', 'Commands', 'CompoundResult', 'Config', 'Core', 'File', 'Result', 'Secrets', 'Session', 'account_arguments',
           'add_step', 'body_param', 'body_value', 'command', 'error_from', 'failed', 'path_param', 'query_param',
           'readable_file', 'strings']
