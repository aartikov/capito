"""OpenAPI discovery and serialization; schema validation belongs to openapi-core."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re
import threading

import yaml

from .view import keys_preview, remaining

METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
FORMAT_EXAMPLES = {"uuid": "00000000-0000-4000-8000-000000000001", "date": "1995-01-01",
                   "date-time": "2026-10-14T10:00:00Z", "uri": "https://example.com/test",
                   "email": "test@example.com", "binary": "@image.png"}
# A name past the depth limit: resolved by finish() into '' when the block defines it, else ' ↓K'.
UNDEFINED = re.compile('\x00([^\x00]+)\x00')


def where(problem, line, column):
    return f'{problem} at line {line} column {column}'


def parse_document(content, label='contract'):
    """An OpenAPI 3 document from YAML or JSON text; the format is recognised by content, not by name.

    Valid JSON is read as JSON: YAML would reject tabs in its indentation and read 1e5 as a string.
    A YAML document fails JSON at its first character and is read as YAML.
    """
    try:
        document = json.loads(content)
    except ValueError as exc:
        json_error = where(exc.msg, exc.lineno, exc.colno) if isinstance(exc, json.JSONDecodeError) else str(exc)
        try:
            document = yaml.safe_load(content)
        except yaml.YAMLError as yaml_exc:
            head = content[:64].decode(errors='ignore') if isinstance(content, bytes) else content[:64]
            if head.lstrip('﻿ \t\r\n').startswith('{'):
                raise ValueError(f'{label}: not a valid JSON document: {json_error}') from None
            mark = getattr(yaml_exc, 'problem_mark', None)
            problem = getattr(yaml_exc, 'problem', None) or type(yaml_exc).__name__
            yaml_error = where(problem, mark.line + 1, mark.column + 1) if mark else problem
            raise ValueError(f'{label}: not a valid YAML document: {yaml_error}') from None
    if not isinstance(document, dict) or not isinstance(document.get('paths'), dict) or not str(document.get('openapi', '')).startswith('3.'):
        raise ValueError(f'{label}: expected an OpenAPI 3 document with paths')
    return document


@dataclass
class Operation:
    id: str
    method: str
    path: str
    spec: dict
    parameters: list
    security: list


class Contract:
    """One OpenAPI document: operations by id and route, schema notation for help, examples."""
    def __init__(self, document):
        self.document = document
        self.operations = {}
        self.by_route = {}
        for route, item in self.document.get("paths", {}).items():
            item = self.deref(item)
            for method, spec in item.items():
                if method not in METHODS:
                    continue
                parameters = {(p["in"], p["name"]): p for p in
                              [self.deref(p) for p in item.get("parameters", []) + spec.get("parameters", [])]}
                op = Operation(spec.get("operationId", method.upper() + " " + route), method.upper(), route,
                               spec, list(parameters.values()), spec.get("security", self.document.get("security", [])))
                if op.id in self.operations:
                    raise ValueError(f"Duplicate operationId: {op.id}")
                self.operations[op.id] = op
                self.by_route[(op.method, route)] = op

    @classmethod
    def from_file(cls, path):
        path = Path(path)
        return cls(parse_document(path.read_bytes(), path.name))

    def deref(self, value):
        while isinstance(value, dict) and "$ref" in value:
            ref = value["$ref"]
            if not ref.startswith("#/"):
                raise ValueError(f"External references are not enabled: {ref}")
            result = self.document
            try:
                for part in ref[2:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    result = result[int(part)] if isinstance(result, list) else result[part]
            except (KeyError, IndexError, TypeError, ValueError):
                raise ValueError(f"Broken reference: {ref} is not in the document") from None
            value = {**result, **{k: v for k, v in value.items() if k != "$ref"}}
        return value

    def get(self, operation, route=None):
        try:
            return self.by_route[(operation.upper(), route)] if route else self.operations[operation]
        except KeyError:
            raise ValueError(f"Unknown operation: {operation} {route or ''}; use client.operations()") from None

    def expanded(self, value, seen=()):
        if isinstance(value, list):
            return [self.expanded(v, seen) for v in value]
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref")
        if ref and ref in seen:
            return value
        return {k: self.expanded(v, seen + ((ref,) if ref else ())) for k, v in self.deref(value).items()}

    def schema_text(self, schema, named=None, indent=0, *, depth=None, items=None):
        """Readable schema for help: one field per line, `name?` optional, `# ...` field description.

        Objects from #/components/schemas print as their name; their definitions collect in `named`
        (name -> (level, text)), shared by every schema of one help block; finish() appends them.
        Without `named`, the returned text is already finished.
        depth counts object levels like view(): the root object is level 0, objects in its fields level 1,
        and so on; arrays and compositions take no level. Objects at level >= depth are not expanded: a named
        one prints its name, an inline one its keys {a, b, c, +N}, both with ↓K levels hidden. A named
        object reached at several levels is defined from the shallowest one. items limits enums.
        """
        own = named is None
        named = {} if own else named
        text = self._notation(schema, named, indent, 0, {'depth': depth, 'items': items})
        return self.finish(text, named) if own else text

    def schema_names(self):
        return list(self.document.get('components', {}).get('schemas', {}))

    def finish(self, text, named):
        """A help block: its text, the definitions it needs, and ↓K after names it leaves undefined."""
        text += ''.join(f'\n{name} = {body}' for name, (_, body) in named.items())
        hidden = lambda match: '' if match[1] in named else remaining(self.levels({'$ref': f'#/components/schemas/{match[1]}'}))
        return UNDEFINED.sub(hidden, text)

    def _notation(self, schema, named, indent, level=0, limits=None):
        limits = limits or {'depth': None, 'items': None}
        ref = schema.get('$ref', '') if isinstance(schema, dict) else ''
        # OAS 3.0 allows null by `nullable: true`, OAS 3.1 by a 'null' type; both read `T | null`.
        null = ' | null' if isinstance(schema, dict) and self.deref(schema).get('nullable') is True else ''
        if ref.startswith('#/components/schemas/'):
            target = self.deref(schema)
            if any(k in target for k in ('properties', 'allOf', 'oneOf', 'anyOf')):
                name = ref.rsplit('/', 1)[1]
                if limits['depth'] is not None and level >= limits['depth']:
                    return f'{name}\x00{name}\x00{null}'  # finish() decides on ↓K once the whole block is known
                defined = named.get(name)
                if defined is None or (limits['depth'] is not None and level < defined[0]):
                    # First or shallower occurrence: (re)define from here; a recursive reference prints the name.
                    named[name] = (level, name)
                    named[name] = (level, self._notation(target, named, 0, level, limits))
                return name + null
        schema = self.deref(schema)
        for key, sep in (('oneOf', ' | '), ('anyOf', ' | '), ('allOf', ' & ')):
            if key in schema:
                return sep.join(self._notation(s, named, indent, level, limits) for s in schema[key]) + null
        typ = schema.get('type')
        nullable = isinstance(typ, list) and 'null' in typ or bool(null)
        if isinstance(typ, list):
            typ = next((t for t in typ if t != 'null'), 'null')
        if 'const' in schema:
            text = json.dumps(schema['const'], ensure_ascii=False)
        elif 'enum' in schema:
            nullable = nullable or None in schema['enum']
            # Values as JSON, the way they are sent (true, 2.5); strings stay bare: DRAFT|ACTIVE.
            values = [v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in schema['enum'] if v is not None]
            shown = values if limits['items'] is None else values[:limits['items']]
            text = '|'.join(shown) + (f"|…+{len(values) - len(shown)} (items={limits['items']})" if len(shown) < len(values) else '')
        elif schema.get('properties') and limits['depth'] is not None and level >= limits['depth']:
            text = keys_preview(schema['properties']) + remaining(self.levels(schema))
        elif typ == 'object' or 'properties' in schema:
            text = self._object(schema, named, indent, level, limits)
        elif typ == 'array':
            bounds = [f'{sign}{schema[key]}' for key, sign in (('minItems', '>='), ('maxItems', '<=')) if key in schema]
            if schema.get('uniqueItems'):
                bounds.append('unique')
            item = self._notation(schema.get('items', {}), named, indent, level, limits)
            item, marker = re.fullmatch(r'(.*?)( ↓\d+|\x00[^\x00]+\x00)?', item, re.S).groups()  # the marker closes the array type
            text = item + '[]' + (f"({', '.join(bounds)})" if bounds else '') + (marker or '')
        else:
            text = schema.get('format') or typ or 'any'
            bounds = [f'{key}={json.dumps(schema[key], ensure_ascii=False) if key in ("pattern", "default") else schema[key]}'
                      for key in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'minLength', 'maxLength',
                                  'pattern', 'multipleOf', 'default') if key in schema]
            if bounds:
                text += f"({', '.join(bounds)})"
        return text + (' | null' if nullable else '')

    def _object(self, schema, named, indent, level=0, limits=None):
        pad = '  ' * (indent + 1)
        required = set(schema.get('required', []))
        lines = []
        for name, value in schema.get('properties', {}).items():
            # A property that references a named object keeps only its own description, not the component's.
            description = value.get('description') if '$ref' in value else self.deref(value).get('description')
            line = f"{pad}{name}{'' if name in required else '?'}: {self._notation(value, named, indent + 1, level + 1, limits)}"
            lines.append(line + (f"  # {' '.join(str(description).split())}" if description else ''))
        extra = schema.get('additionalProperties')
        if isinstance(extra, dict):
            if not lines:
                return f'dict[string, {self._notation(extra, named, indent, level + 1, limits)}]'
            lines.append(f'{pad}*: {self._notation(extra, named, indent + 1, level + 1, limits)}')
        if not lines:
            # A closed object without properties rejects every field the server may send.
            return 'object(closed)' if extra is False else 'object'
        return '{\n' + '\n'.join(lines) + '\n' + '  ' * indent + '}'

    def levels(self, schema, seen=frozenset()):
        """Object levels from this schema down to its deepest field, counted like depth; a cycle counts once."""
        ref = schema.get('$ref', '') if isinstance(schema, dict) else ''
        if ref in seen:
            return 1
        seen = seen | {ref} if ref else seen
        schema = self.deref(schema)
        if not isinstance(schema, dict):
            return 0
        parts = [self.levels(s, seen) for key in ('oneOf', 'anyOf', 'allOf') for s in schema.get(key, [])]
        if 'items' in schema:
            parts.append(self.levels(schema['items'], seen))
        fields = list(schema.get('properties', {}).values())
        if isinstance(schema.get('additionalProperties'), dict):
            fields.append(schema['additionalProperties'])
        if fields:
            parts.append(1 + max(self.levels(f, seen) for f in fields))
        return max(parts, default=0)

    def example(self, schema, depth=0):
        """A value for help examples: the contract's own example if any, else one that satisfies the schema's bounds.

        A string with a pattern and no example of its own is a placeholder <pattern …>: a pattern cannot be satisfied in general.
        """
        if depth > 8:
            return None
        schema = self.deref(schema)
        for key in ("example", "default", "const"):
            if key in schema:
                return schema[key]
        if schema.get("examples"):
            return schema["examples"][0]
        if "enum" in schema:
            return schema["enum"][0]
        if "allOf" in schema:
            values = [self.example(s, depth + 1) for s in schema["allOf"]]
            return {k: v for value in values if isinstance(value, dict) for k, v in value.items()}
        for key in ("oneOf", "anyOf"):
            if key in schema:
                return self.example(schema[key][0], depth + 1)
        typ = schema.get("type")
        if isinstance(typ, list):
            typ = next((t for t in typ if t != "null"), "null")
        if typ == "object" or "properties" in schema:
            return {k: self.example(v, depth + 1) for k, v in schema.get("properties", {}).items()
                    if k in schema.get("required", schema.get("properties", {}))}
        if typ == "array":
            count = schema.get("minItems", 1)
            count = count if "maxItems" not in schema else min(count, schema["maxItems"])
            return [self.example(schema.get("items", {}), depth + 1)] * count
        if typ in ("integer", "number"):
            return number_example(schema, typ)
        if typ == "boolean":
            return True
        if schema.get("format") in FORMAT_EXAMPLES:
            return FORMAT_EXAMPLES[schema["format"]]
        if "pattern" in schema:
            return f"<pattern {schema['pattern']}>"
        return "example".ljust(schema.get("minLength", 0), "x")[:schema.get("maxLength")]


def number_example(schema, typ):
    """1, or the nearest value within the bounds, rounded up to multipleOf; OAS 3.0 boolean exclusive bounds included."""
    low, high = schema.get("minimum"), schema.get("maximum")
    exclusive_low, exclusive_high = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
    step = 1 if typ == "integer" else 0.5
    if exclusive_low is True and low is not None:
        low = low + step
    elif not isinstance(exclusive_low, bool) and exclusive_low is not None:
        low = exclusive_low + step if low is None else max(low, exclusive_low + step)
    if exclusive_high is True and high is not None:
        high = high - step
    elif not isinstance(exclusive_high, bool) and exclusive_high is not None:
        high = exclusive_high - step if high is None else min(high, exclusive_high - step)
    value = 1 if low is None else low
    if high is not None and value > high:
        value = high
    if schema.get("multipleOf"):
        multiple = schema["multipleOf"]
        value = -(-value // multiple) * multiple
    return int(value) if typ == "integer" else value


class ContractSource:
    """A configured contract: a local path or an http(s) URL, read once on first use and kept in memory.

    Nothing is read when the client is created; a URL is fetched with the client's own transport.
    """
    def __init__(self, name, location, fetch=None):
        self.name, self.location, self._fetch = name, location, fetch
        self._guard = threading.Lock()
        self._loaded = False
        self.contract = self.sha256 = self.error = None

    @property
    def remote(self):
        return isinstance(self.location, str) and self.location.lower().startswith(('http://', 'https://'))

    def load(self):
        """The Contract; ValueError with the reason when it cannot be read or parsed."""
        with self._guard:
            if not self._loaded:
                self._loaded = True
                try:
                    content = self._fetch(self.location) if self.remote else Path(self.location).read_bytes()
                    self.sha256 = hashlib.sha256(content).hexdigest()
                    self.contract = Contract(parse_document(content, self.name))
                except Exception as exc:  # whatever the reason, it is remembered: a half-read contract is never used
                    reason = str(exc) if isinstance(exc, (OSError, ValueError)) else f'{type(exc).__name__}: {exc}'
                    self.error = f'Contract {self.name} ({self.location}) is unavailable: {reason}'
        if self.error:
            raise ValueError(self.error)
        return self.contract

    def info(self):
        """Provenance: source, and after loading the version and SHA-256 of what was read, or why it failed."""
        entry = {'source': str(self.location)}
        if self.contract is not None:
            entry.update(version=self.contract.document.get('info', {}).get('version'), sha256=self.sha256)
        elif self.error:
            entry['unavailable'] = self.error
        else:
            entry['loaded'] = False
        return entry
