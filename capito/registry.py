"""Decorator-backed discovery; descriptions never execute command bodies."""
import difflib
import inspect
import json
import re

from .files import File
from .transport import media_type, scalar as text_value
from .view import ALL, UNSET, ViewList, cap, project, scalar


RESULT_TYPES = {'http': 'Result', 'compound': 'CompoundResult', 'account': 'Account',
                'accounts': 'list[Account]', 'session': 'Session', 'text': 'str', 'none': 'None'}
# An operation of the help contract as commands name it: api:<METHOD> <route>.
API_REFERENCE = re.compile(r'api:[A-Za-z]+ /\S*')
# Header parameters OpenAPI says to ignore, as content, responses and security set these headers: help shows
# them as the contract has them, examples leave them out; a required Authorization still marks that sign-in is needed.
IGNORED_HEADERS = {'accept', 'content-type', 'authorization'}
# Their meaning is the same for every command and is documented once in docs/usage.md.
STANDARD_ARGUMENTS = ('as_user', 'validate')


def bodiless(method, code):
    """A response HTTP forbids a body for: to HEAD, 1xx, 204 and 304."""
    return method.upper() == 'HEAD' or code in ('204', '304') or code.startswith('1')


def command(summary, *, api=(), effects='', example='', parameters=None, result='http',
            steps=(), data=None, prerequisites=(), notes=()):
    """Describe a command beside its implementation; schema_from links args to OpenAPI.

    A command has an explicit signature: its own arguments, as_user and validate.
    Anything lower-level (headers, raw bodies, session snapshots) belongs to client.call().
    Metadata is help-only: effects, prerequisites and notes are what this command adds to docs/usage.md.
    """
    if result not in RESULT_TYPES:
        raise ValueError(f'Unknown result kind: {result}')
    if isinstance(api, str):
        raise TypeError(f"api= is a tuple of operations: api=({api!r},)")
    def decorate(fn):
        fn.__command__ = dict(summary=summary, api=list(api), effects=effects, example=example,
                              parameters=parameters or {}, result=result, steps=list(steps),
                              data=data or {}, prerequisites=list(prerequisites), notes=list(notes))
        return fn
    return decorate


def body_param(api, description=None):
    """Help metadata for a command argument that becomes the request body of api."""
    return {'type': 'dict', 'schema_from': (api, 'body'), **({'description': description} if description else {})}


def query_param(api, **extra):
    """Help metadata for the query argument of api."""
    return {'type': 'dict | None', 'schema_from': (api, 'query'), **extra}


def path_param(api, name, description):
    """Help metadata for a command argument that fills the {name} template of api."""
    return {'type': 'str', 'schema_from': (api, 'path', name), 'description': description}


def is_file(schema):
    """A schema of file content: format binary, or contentMediaType of OAS 3.1."""
    return isinstance(schema, dict) and (schema.get('format') == 'binary' or 'contentMediaType' in schema)


def form_example(schema, values):
    """Form values as call(multipart=...) takes them: a file field is File('upload.bin'), a list of files a list of them."""
    def field(part, value):
        if is_file(part):
            return File('upload.bin')
        if part.get('type') == 'array' and is_file(part.get('items')):
            return [File('upload.bin')] * max(len(value) if isinstance(value, list) else 1, 1)
        return value
    properties = schema.get('properties', {})
    return {name: field(properties.get(name, {}), value) for name, value in values.items()}


def query_example(parameters, values):
    """Query values as call() takes them: an object parameter spread into keys by its style.

    call() sends only scalars and lists, so filter={'status': 'a'} becomes filter[status]=a for deepObject,
    status=a for an exploded form and filter=status,a for a form without explode; another style is a placeholder.
    """
    styles = {p['name']: (p.get('style', 'form'), p.get('explode', p.get('style', 'form') == 'form')) for p in parameters}
    query = {}
    for name, value in values.items():
        style, explode = styles.get(name, ('form', True))
        if not isinstance(value, dict):
            query[name] = value
        elif style == 'deepObject':
            query |= {f'{name}[{key}]': item for key, item in value.items()}
        elif style == 'form' and explode:
            query |= value
        elif style == 'form' and all(not isinstance(item, (dict, list)) for item in value.values()):
            query[name] = ','.join(f'{key},{text_value(item)}' for key, item in value.items())
        else:
            query[name] = f'<{name}: an object, encode it by style {style}{"" if explode else ", explode false"}>'
    return query


def check_declaration(identifier, fn, meta):
    """Errors in a command's own declaration, found when the client is created (no contract needed)."""
    arguments = inspect.signature(fn).parameters
    unknown = sorted(set(meta['parameters']) - set(arguments))
    if unknown:
        raise TypeError(f'{identifier}: parameters= describes no such arguments: {", ".join(unknown)}')
    for name in arguments:
        doc = meta['parameters'].get(name, {})
        if name not in STANDARD_ARGUMENTS and not (doc.get('description') or 'schema_from' in doc):
            raise TypeError(f'{identifier}: describe argument {name!r} in @command(parameters=...)')
    if meta['result'] == 'compound' and not (meta['steps'] and meta['data']):
        raise TypeError(f'{identifier}: a compound command declares steps and data')
    sources = [doc['schema_from'] for doc in meta['parameters'].values() if 'schema_from' in doc]
    for reference in [*meta['api'], *(source[0] for source in sources)]:
        if not isinstance(reference, str) or not API_REFERENCE.fullmatch(reference):
            raise TypeError(f"{identifier}: {reference!r} must look like 'api:GET /items/{{itemId}}'")
    for source in sources:
        if source[1] not in ('path', 'query', 'body') or (source[1] == 'path' and len(source) < 3):
            raise TypeError(f'{identifier}: schema_from {source!r} takes the body, the query or a named path parameter')


def one_line(text):
    return ' '.join(str(text).split())


def suggestion(name, candidates):
    close = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    return f"; did you mean {', '.join(close)}?" if close else ''


class Operation:
    """One row of operations(): a command or a contract operation."""
    def __init__(self, id, kind, summary, *, command=None, signature=None, effects=None, limits=ALL):
        self.id, self.kind, self.summary = id, kind, summary
        self.command, self.signature, self.effects = command, signature, effects
        self._limits = limits

    def view(self, *paths, depth=UNSET, width=UNSET, format='text'):
        if format == 'json':
            fields = dict(id=self.id, kind=self.kind, summary=self.summary, command=self.command,
                          signature=self.signature, effects=self.effects)
            tree = {key: value for key, value in fields.items() if value is not None}
            return json.dumps(project(tree, paths) if paths else tree, ensure_ascii=False, indent=2)
        if format != 'text':
            raise ValueError("format is 'text' or 'json'")
        o = self._limits.resolve(depth=depth, width=width)
        depth, width = o['depth'], o['width']
        line = f'{self.id}  {self.summary}' + (f'  → {self.command}' if self.command else '')
        if (depth is None or depth > 0) and self.signature:
            line += f'\n    {self.signature}'
            if self.effects:
                line += f'\n    effects: {scalar(self.effects, width)}'
        return line

    def __repr__(self):
        return self.view()

    __str__ = __repr__


class Help:
    """A help card. print(help) is help.view(); fields are attributes rendered with the [view.help] limits."""
    FIELDS = ('id', 'summary', 'description', 'auth', 'call', 'request', 'schema', 'api', 'effects',
              'prerequisites', 'returns', 'example', 'commands')

    def __init__(self, build, limits=ALL):
        self._build = build
        self._limits = limits
        self._default = None

    @property
    def fields(self):
        """Card fields as strings, built with the configured help depth and items."""
        if self._default is None:
            self._default = self._build(self._limits.depth, self._limits.items)
        return self._default

    def __getattr__(self, name):
        if name in Help.FIELDS:
            return self.fields.get(name)
        raise AttributeError(name)

    def view(self, *fields, depth=UNSET, items=UNSET, width=UNSET, lines=UNSET, format='text'):
        """The card with named schemas defined `depth` levels deep; fields selects card keys.

        A limit not passed comes from [view.help] of the configuration; 'all' lifts it.
        format='json' is the complete card, without limits, like view(format='json') of a result.
        """
        o = self._limits.resolve(depth=depth, items=items, width=width, lines=lines)
        if format == 'json':
            card = self._build(None, None)
            return json.dumps({k: card.get(k) for k in fields} if fields else card, ensure_ascii=False, indent=2)
        width, lines = o['width'], o['lines']
        card = self._build(o['depth'], o['items'])
        out = [card['id'] + (f" — {card['summary']}" if card.get('summary') else '')]
        for key in fields or [k for k in card if k not in ('id', 'summary')]:
            text = card.get(key, '<missing>')
            first, *rest = text.split('\n')
            out.append(f'{key}: {first}')
            out.extend(rest)
        if width is not None:
            out = [line if len(line) <= width else f'{line[:width]}…(+{len(line) - width}, width={width})' for line in out]
        return '\n'.join(cap(out, lines))

    def __repr__(self):
        return self.view()

    __str__ = __repr__


class Registry:
    """Commands of the client and help built from them and the help contract, read on first use."""
    def __init__(self, contract, *, help_limits=ALL, list_limits=ALL, accounts=False):
        self._contract = contract  # a Contract, or a callable returning it
        self.help_limits, self.list_limits = help_limits, list_limits
        self.accounts = accounts  # the client has an accounts set, so examples may use as_user
        self.commands = {}
        self._references_checked = False

    @property
    def contract(self):
        return self._contract() if callable(self._contract) else self._contract

    def register(self, set_name, instance):
        for name, fn in inspect.getmembers(instance, callable):
            meta = getattr(fn, '__command__', None)
            if meta is None:
                continue
            identifier = f'command:{set_name}.{name}'
            if any(p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL) for p in inspect.signature(fn).parameters.values()):
                raise ValueError(f'{identifier}: declare arguments explicitly; low-level settings belong to client.call()')
            if identifier in self.commands:
                raise ValueError(f'Duplicate command: {identifier}')
            check_declaration(identifier, fn, meta)
            self.commands[identifier] = (fn, meta)
        self._references_checked = False

    def check_references(self):
        """Once per contract: every api= and schema_from reference of every command exists in the help contract.

        Help is built from these references, so a broken one is an error of help, listed for all commands at once.
        Calls and validation do not depend on it.
        """
        if self._references_checked:
            return
        problems = []
        for identifier, (_, meta) in sorted(self.commands.items()):
            problems += [f'{identifier}: {reference} is not in the help contract'
                         for reference in meta['api'] if self.operation(reference) is None]
            for name, doc in meta['parameters'].items():
                if 'schema_from' in doc:
                    problem = self._reference_problem(doc['schema_from'])
                    if problem:
                        problems.append(f'{identifier}: argument {name}: {problem}')
        if problems:
            raise ValueError('Command declarations disagree with the help contract:\n  ' + '\n  '.join(problems))
        self._references_checked = True

    def _reference_problem(self, reference):
        identifier, location, *name = reference
        op = self.operation(identifier)
        if op is None:
            return f'{identifier} is not in the help contract'
        if location == 'path' and not any(p['in'] == 'path' and p['name'] == name[0] for p in op.parameters):
            return f'{identifier} has no path parameter {name[0]!r}'
        if location == 'query' and not any(p['in'] == 'query' for p in op.parameters):
            return f'{identifier} has no query parameters'
        if location == 'body':
            body = self.contract.deref(op.spec.get('requestBody', {})).get('content', {})
            if not body:
                return f'{identifier} has no request body'
            if name and name[0] not in self.contract.expanded(self._source(op, 'body', [])).get('properties', {}):
                return f'{identifier} body has no field {name[0]!r}'
        return None

    def operations(self, text=None, *, kind=None):
        self.check_references()
        if kind not in (None, 'command', 'api'):
            raise ValueError("kind is 'command', 'api' or None")
        rows = []
        if kind != 'api':
            for key, (fn, meta) in sorted(self.commands.items()):
                rows.append(Operation(key, 'command', meta['summary'], effects=one_line(meta['effects']) or None,
                                      signature=f"client.{key.removeprefix('command:')}{inspect.signature(fn)}",
                                      limits=self.list_limits))
        if kind != 'command':
            raw = []
            for op in self.contract.by_route.values():
                identifier = f'api:{op.method} {op.path}'
                wrappers = [key for key, (_, meta) in sorted(self.commands.items()) if identifier in meta['api']
                            and len(meta['api']) == 1]
                raw.append(Operation(identifier, 'api', op.spec.get('summary', op.id), command=', '.join(wrappers) or None,
                                     limits=self.list_limits))
            rows += sorted(raw, key=lambda row: row.id)
        if text:
            needle = text.casefold()
            rows = [r for r in rows if needle in r.id.casefold() or needle in r.summary.casefold()]
        return ViewList(rows, limits=self.list_limits)

    def operation(self, identifier):
        """Contract operation for an api: identifier, or None when the contract lacks it."""
        method, route = identifier.removeprefix('api:').split(' ', 1)
        return self.contract.by_route.get((method.upper(), route))

    def schema(self, reference):
        """Expanded OpenAPI schema behind a schema_from reference (api, location, *name)."""
        identifier, location, *name = reference
        op = self.operation(identifier)
        if op is None:
            raise ValueError(f'{identifier} is absent from the help contract')
        return self.contract.expanded(self._source(op, location, name))

    def _body_media(self, op):
        """The request body type help describes: application/json if the operation has it, else its first type."""
        content = self.contract.deref(op.spec.get('requestBody', {})).get('content', {})
        return 'application/json' if 'application/json' in content else next(iter(content), None)

    def _source(self, op, location, name):
        """Schema behind a reference with its $refs intact, so help can print named objects."""
        if location == 'body':
            content = self.contract.deref(op.spec.get('requestBody', {})).get('content', {})
            schema = content.get('application/json', next(iter(content.values()), {})).get('schema', {})
            return self.contract.deref(schema).get('properties', {}).get(name[0], {}) if name else schema
        params = [p for p in op.parameters if p['in'] == location]
        if location == 'query':
            return {'type': 'object', 'properties': {p['name']: p.get('schema', {}) for p in params},
                    'required': [p['name'] for p in params if p.get('required')]}
        return next(p for p in params if p['name'] == name[0]).get('schema', {})

    def _parameter(self, p, meta):
        """Internal parameter record: drives the example and the help line."""
        doc = dict(meta.get(p.name, {}))
        if p.name == 'as_user':
            doc.setdefault('example', 'alice')
        row = dict(name=p.name, kind=p.kind.name, required=p.default is inspect.Parameter.empty, **doc)
        if p.default is not inspect.Parameter.empty:
            row['default'] = p.default
        if 'schema_from' in doc:
            schema = self.schema(doc['schema_from'])
            row['schema'] = schema
            example = self.contract.example(schema)
            op, location, *field = self.operation(doc['schema_from'][0]), *doc['schema_from'][1:]
            if location == 'query' and isinstance(example, dict):
                example = query_example([p for p in op.parameters if p['in'] == 'query'], example)
            if location == 'body' and media_type(self._body_media(op) or '') == 'multipart/form-data':
                whole = {'properties': {field[0]: schema}} if field else schema
                example = form_example(whole, {field[0]: example} if field else example)
                example = example[field[0]] if field else example
            if example not in ({}, None):
                row.setdefault('example', example)
        return row

    def _parameter_line(self, row, named, limits):
        """`name: type — description`; a multi-line schema goes below the signature as `name = ...`."""
        reference = row.get('schema_from')
        description = row.get('description')
        block = None
        if reference:
            op = self.operation(reference[0])
            text = self.contract.schema_text(self._source(op, reference[1], reference[2:]), named, 0, **limits)
            if not description and reference[1] in ('body', 'query'):
                description = ('body of ' if reference[1] == 'body' else 'query of ') + reference[0].removeprefix('api:')
            if '\n' in text:  # an inline object: shown below the parameter lines
                block = f"{row['name']} = {text}"
                type_ = row.get('type')
            else:
                type_ = text
        else:
            type_ = row.get('type')
        line = '  ' + row['name'] + (f': {type_}' if type_ else '') + (f' — {one_line(description)}' if description else '')
        return line, block

    @staticmethod
    def _command_example(name, parameters, needs_auth):
        args = []
        for p in parameters:
            schema_required = p.get('schema', {}).get('required', [])
            if not (p['required'] or p.get('include_in_example') or schema_required):
                continue
            value = p.get('example', p.get('default'))
            if value is None and p['required']:
                value = f'<{p["name"]}>'
            args.append(repr(value) if p['kind'] == 'POSITIONAL_ONLY' else f'{p["name"]}={value!r}')
        optional_as_user = any(p['name'] == 'as_user' and not p['required'] for p in parameters)
        if needs_auth and optional_as_user:
            args.append("as_user='alice'")
        return f'client.{name}(' + ', '.join(args) + ')'

    @staticmethod
    def _asks_authorization(op):
        """A required Authorization header parameter: OpenAPI ignores it, yet the operation clearly needs sign-in."""
        return any(p['in'] == 'header' and p['name'].lower() == 'authorization' and p.get('required') for p in op.parameters)

    def _needs_auth(self, op):
        return bool(op.security) or self._asks_authorization(op)

    def _credentials(self, op):
        """Placeholders for the first way the contract allows to authorize op, as explicit arguments.

        Returns (headers, query, cookies); nothing when the operation also allows anonymous calls.
        """
        headers, query, cookies = {}, {}, []
        if not op.security:  # a contract that asks for Authorization as a parameter rather than by security
            return ({'Authorization': '<credentials>'} if self._asks_authorization(op) else {}), query, cookies
        if not all(op.security):
            return headers, query, cookies
        schemes = self.contract.document.get('components', {}).get('securitySchemes', {})
        for name in op.security[0]:
            scheme = self.contract.deref(schemes.get(name, {}))
            kind, place = scheme.get('type'), scheme.get('in')
            if kind == 'apiKey' and place == 'query':
                query[scheme['name']] = '<api key>'
            elif kind == 'apiKey' and place == 'cookie':
                cookies.append(f"{scheme['name']}=<api key>")
            elif kind == 'apiKey':
                headers[scheme['name']] = '<api key>'
            elif kind == 'http' and str(scheme.get('scheme', '')).lower() != 'bearer':
                headers['Authorization'] = f"{str(scheme.get('scheme', '')).title()} <credentials>"
            elif kind in ('http', 'oauth2', 'openIdConnect'):
                headers['Authorization'] = 'Bearer <token>'
        return headers, query, cookies

    def _parameter_example(self, p, *, as_text):
        """The parameter's example value; a header or cookie carries it as text."""
        value = p.get('example', self.contract.example(p.get('schema', {})))
        return text_value(value) if as_text else value

    def _media_example(self, media, schema):
        """The example given for the media type, else one built from its schema."""
        if 'example' in media:
            return media['example']
        for example in media.get('examples', {}).values():
            example = self.contract.deref(example)
            if 'value' in example:  # an externalValue is a link, not a value
                return example['value']
        return self.contract.example(schema)

    def _raw_example(self, op):
        # With accounts the example signs in by as_user; without them as_user is an error, so the
        # credentials the contract asks for are spelled out with placeholders.
        auth_headers, auth_query, auth_cookies = ({}, {}, []) if self.accounts else self._credentials(op)
        values = {}
        for location, target, credentials in [('path', 'path', {}), ('query', 'query', auth_query), ('header', 'headers', auth_headers)]:
            fields = {p['name']: self._parameter_example(p, as_text=location == 'header')
                      for p in op.parameters if p['in'] == location and p.get('required')
                      and not (location == 'header' and p['name'].lower() in IGNORED_HEADERS)} | credentials
            if location == 'query':
                fields = query_example([p for p in op.parameters if p['in'] == 'query'], fields)
            if fields:
                values[target] = fields
        cookies = [f"{p['name']}={self._parameter_example(p, as_text=True)}"
                   for p in op.parameters if p['in'] == 'cookie' and p.get('required')] + auth_cookies
        if cookies:
            values.setdefault('headers', {})['Cookie'] = '; '.join(cookies)
        content = self.contract.deref(op.spec.get('requestBody', {})).get('content', {})
        if content:
            media = 'application/json' if 'application/json' in content else next(iter(content))
            schema = self.contract.expanded(content[media].get('schema', {}))
            value = self._media_example(content[media], schema)
            kind = media_type(media)
            if kind == 'multipart/form-data' and isinstance(value, dict):
                values['multipart'] = form_example(schema, value)
            elif (kind == 'application/json' or kind.endswith('+json') or (isinstance(value, str) and not is_file(schema))
                  or (kind == 'application/x-www-form-urlencoded' and isinstance(value, dict))):
                values['body'] = value
            else:  # call() encodes JSON, forms and text only; any other body, a file's included, is ready bytes
                values['raw'] = f'<{kind} body>'.encode()
            if media != 'application/json' and 'multipart' not in values:
                values['content_type'] = media
        if self._needs_auth(op) and self.accounts:
            values['as_user'] = 'alice'
        args = [repr(op.method), repr(op.path)] + [f'{k}={v!r}' for k, v in values.items()]
        return 'client.call(' + ', '.join(args) + ')'

    def _bodies(self, op, named, success_only, limits):
        """Lines `body on <codes> (<description>): <schema>`; codes with the same body share a line."""
        groups = {}
        for code, response in op.spec.get('responses', {}).items():
            code = str(code)
            if success_only and not code.startswith('2'):
                continue
            response = self.contract.deref(response)
            content = response.get('content', {})
            if content:
                media = 'application/json' if 'application/json' in content else next(iter(content))
                if 'schema' in content[media]:
                    text = self.contract.schema_text(content[media]['schema'], named, 1, **limits)
                    if media != 'application/json':
                        text += f' [{media}]'
                else:  # the type is known, the structure is not: validation reports such a body as not_described
                    text = f'not described [{media}]'
            else:
                text = 'none' if bodiless(op.method, code) else 'not described'
            description = one_line(response.get('description', ''))
            groups.setdefault((description, text), []).append(code)
        return [f"  body on {', '.join(codes)}" + (f' ({description})' if description else '') + f': {text}'
                for (description, text), codes in groups.items()]

    def describe(self, identifier):
        """Help card for command:<set>.<name>, api:<METHOD> <route> or schema:<Name>; nothing runs."""
        self.check_references()
        if identifier.startswith('command:'):
            if identifier not in self.commands:
                raise ValueError(f'Unknown command: {identifier}{suggestion(identifier, self.commands)}; use operations()')
            return Help(lambda depth, items: self._describe_command(identifier, dict(depth=depth, items=items)), self.help_limits)
        if identifier.startswith('api:'):
            method, _, route = identifier[4:].partition(' ')
            op = self.contract.by_route.get((method.upper(), route))
            if op is None:
                known = [f'api:{m} {r}' for m, r in self.contract.by_route]
                raise ValueError(f'Unknown operation: {identifier}{suggestion(identifier, known)}; use operations()')
            return Help(lambda depth, items: self._describe_operation(identifier, op, dict(depth=depth, items=items)), self.help_limits)
        if identifier.startswith('schema:'):
            name = identifier.removeprefix('schema:')
            names = self.contract.schema_names()
            if name not in names:
                raise ValueError(f'Unknown schema: {name}{suggestion(name, names)}')
            return Help(lambda depth, items: self._describe_schema(identifier, name, dict(depth=depth, items=items)), self.help_limits)
        raise ValueError('Use command:<set>.<name>, api:<METHOD> <route> or schema:<Name>')

    def _describe_schema(self, identifier, name, limits):
        target = self.contract.document['components']['schemas'][name]
        help_ = {'id': identifier}
        if one_line(target.get('description', '')):
            help_['summary'] = one_line(target['description'])
        # The schema itself is level 0: depth=0 leaves only its name, otherwise the text opens with `Name = {…}`.
        text = self.contract.schema_text({'$ref': f'#/components/schemas/{name}'}, **limits)
        help_['schema'] = text.removeprefix(f'{name}\n') if text.startswith(f'{name}\n{name} = ') else text
        return help_

    def _describe_command(self, identifier, limits):
        fn, meta = self.commands[identifier]
        sig = inspect.signature(fn)
        name = identifier.removeprefix('command:')
        parameters = [self._parameter(p, meta['parameters']) for p in sig.parameters.values()]
        operations = [op for op in map(self.operation, meta['api']) if op]
        needs_auth = any(self._needs_auth(op) for op in operations)

        named, lines, blocks = {}, [f'client.{name}{sig}'], []
        for row in parameters:
            if row['name'] in STANDARD_ARGUMENTS and 'description' not in row:
                continue
            line, block = self._parameter_line(row, named, limits)
            lines.append(line)
            if block:
                blocks.append(block)
        help_ = {'id': identifier, 'summary': meta['summary']}
        if inspect.getdoc(fn):
            help_['description'] = inspect.getdoc(fn)
        help_['call'] = self.contract.finish('\n'.join(lines + blocks), named)
        if meta['api']:
            help_['api'] = ', '.join(meta['api'])
        for key in ('effects', 'prerequisites'):
            if meta[key]:
                help_[key] = meta[key] if isinstance(meta[key], str) else '\n'.join(meta[key])
        help_['returns'] = self._command_returns(meta, operations, limits)
        help_['example'] = meta['example'] or self._command_example(name, parameters, needs_auth and self.accounts)
        return help_

    def _command_returns(self, meta, operations, limits):
        lines, named = [RESULT_TYPES[meta['result']]], {}
        if meta['result'] == 'http' and len(operations) == 1:
            lines += self._bodies(operations[0], named, True, limits)
        if meta['steps']:
            lines += ['  steps:'] + [f'    {step}' for step in meta['steps']]
        if meta['data']:
            lines += ['  data:'] + [f'    {key} — {text}' for key, text in meta['data'].items()]
        lines += [f'  note: {note}' for note in meta['notes']]
        return self.contract.finish('\n'.join(lines), named)

    def _describe_operation(self, identifier, op, limits):
        named = {}
        help_ = {'id': identifier, 'summary': op.spec.get('summary') or op.id}
        if op.spec.get('description'):
            help_['description'] = op.spec['description'].strip()
        help_['auth'] = ' or '.join(' + '.join(requirement) or 'none' for requirement in op.security) or 'none'
        request = []
        for p in op.parameters:
            optional = '' if p.get('required') else '?'
            description = f"  # {one_line(p['description'])}" if p.get('description') else ''
            request.append(f"{p['in']} {p['name']}{optional}: {self.contract.schema_text(p.get('schema', {}), named, **limits)}{description}")
        body = self.contract.deref(op.spec.get('requestBody', {}))
        for media, content in body.get('content', {}).items():
            optional = '' if body.get('required') else '?'
            request.append(f"body{optional} [{media}]: {self.contract.schema_text(content.get('schema', {}), named, **limits)}")
        if request:
            help_['request'] = self.contract.finish('\n'.join(request), named)
        named = {}
        help_['returns'] = self.contract.finish('\n'.join(['Result'] + self._bodies(op, named, False, limits)), named)
        help_['example'] = self._raw_example(op)
        commands = [key for key, (_, meta) in sorted(self.commands.items()) if identifier in meta['api']]
        if commands:
            help_['commands'] = ', '.join(commands)
        return help_
