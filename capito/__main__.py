"""Help from the shell: python -m capito operations|describe|docs. Read-only; requests are made from Python."""
import argparse
from importlib import resources
import json
import sys

from .client import client_class
from .config import Config

DOCS = {'usage': 'Working with an API: find an operation, call it, read the result, accounts, validation',
        'extending': 'Connecting your API: Client, commands, authorization, accounts, tests'}


def limit(value):
    """A view limit for the command line: a non-negative number or 'all', passed to view() as is."""
    if value == 'all':
        return value
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative number or 'all', got {value!r}")
    return number


def load_client(path=None, environment=None):
    """The client named by the `client` key of capito.toml (given path or found upwards); capito.Client without it."""
    config = Config.load(path, environment)
    return client_class(config)(config)


def config_options(target, *, defaults=True):
    """--config and --env, accepted before and after the subcommand; without defaults the subcommand never resets them."""
    config, env = (None, None) if defaults else (argparse.SUPPRESS, argparse.SUPPRESS)
    target.add_argument('--config', default=config, help='path to capito.toml; default: the nearest one upwards from the working directory')
    target.add_argument('--env', default=env, help='environment; default: default_environment')


def parser():
    common = argparse.ArgumentParser(add_help=False)
    config_options(common, defaults=False)
    limits = argparse.ArgumentParser(add_help=False)
    for name, text in (('depth', 'levels under each selected node'), ('width', 'characters per value'), ('lines', 'lines of output')):
        limits.add_argument(f'--{name}', type=limit, default=argparse.SUPPRESS, metavar='N|all',
                            help=f'{text}; all for no limit; default: [view] of the configuration')

    root = argparse.ArgumentParser(prog='python -m capito',
                                   description='Help on commands, contract operations and schemas, and the capito documentation. '
                                               'Makes no API requests and changes no state.')
    config_options(root)
    sub = root.add_subparsers(dest='action', required=True)
    ops = sub.add_parser('operations', parents=[common, limits], help='list operations: client.operations(text, kind=).view()',
                         description='Commands and contract operations; --depth 1 adds signatures and effects.')
    ops.add_argument('text', nargs='?', help='substring of id or summary, case-insensitive')
    ops.add_argument('--kind', choices=('command', 'api'))
    card = sub.add_parser('describe', parents=[common, limits], help='help cards: client.describe(id).view()',
                          description='Cards for command:<set>.<command>, "api:<METHOD> <route>" or schema:<Name>.')
    card.add_argument('ids', nargs='+', metavar='id')
    card.add_argument('--fields', nargs='+', default=(), metavar='FIELD', help='only these card fields, e.g. call returns')
    card.add_argument('--items', type=limit, default=argparse.SUPPRESS, metavar='N|all', help='enum values; all for no limit')
    card.add_argument('--json', action='store_true', help='JSON without limits; several ids give an array')
    docs = sub.add_parser('docs', help='capito documentation; no configuration needed',
                          description='Documentation of the installed capito version. Without a topic: the list of topics.')
    docs.add_argument('topic', nargs='?', choices=tuple(DOCS))
    return root


def show_docs(topic):
    if topic is None:
        print('\n'.join(f'{name:10} {text}' for name, text in DOCS.items()))
        print('\npython -m capito docs <topic>')
        return 0
    print(resources.files('capito').joinpath('docs', f'{topic}.md').read_text(encoding='utf-8'))
    return 0


def main(argv=None, *, make_client=load_client):
    args = parser().parse_args(argv)
    if args.action == 'docs':
        return show_docs(args.topic)
    options = {k: getattr(args, k) for k in ('depth', 'items', 'width', 'lines') if hasattr(args, k)}
    try:
        client = make_client(args.config, environment=args.env)
    except (OSError, ValueError, KeyError, ImportError, AttributeError) as exc:
        print(f'capito: cannot load the client: {exc}', file=sys.stderr)
        return 2
    try:
        if args.action == 'operations':
            print(client.operations(args.text, kind=args.kind).view(**options))
            return 0
    except ValueError as exc:  # the help contract cannot be read
        print(f'capito: {exc}', file=sys.stderr)
        return 2
    cards, failed = [], False
    for identifier in args.ids:
        try:
            help_ = client.describe(identifier)
            cards.append(help_.view(*args.fields, **options, format='json' if args.json else 'text'))
        except ValueError as exc:
            print(f'capito: {exc}', file=sys.stderr)
            failed = True
    if args.json:
        parsed = [json.loads(c) for c in cards]
        if parsed:
            print(json.dumps(parsed[0] if len(args.ids) == 1 else parsed, ensure_ascii=False, indent=2))
    elif cards:
        print('\n\n'.join(cards))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
