"""Text views for reading results: select paths, expand to a depth, cap the size.

Rendering never changes data. Objects that render themselves (results, accounts) implement
summary() and view_tree(); the tree they return is already masked by their own secret policy.
"""
from dataclasses import dataclass
import json
import re

KEYS_SHOWN = 3
LIMIT_NAMES = ('depth', 'items', 'width', 'lines')
KINDS = ('result', 'help', 'list', 'account')  # [view.<kind>] in capito.toml


class _Unset:
    """Default of view() limits: the value configured for the object's kind."""
    def __repr__(self):
        return '<from config>'


UNSET = _Unset()


def limit_value(name, value):
    """A configured or passed limit: a non-negative integer, or 'all' (stored as None) for no limit."""
    if value == 'all':
        return None
    if value is None:
        raise TypeError(f'{name}=None is not a limit; use {name}="all" for no limit')
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or 'all', got {value!r}")
    return value


@dataclass(frozen=True)
class Limits:
    """Default limits of one kind of object; None means no limit."""
    depth: int | None = None
    items: int | None = None
    width: int | None = None
    lines: int | None = None

    def resolve(self, **passed):
        """Limits for one view() call: UNSET takes the default, 'all' lifts the limit."""
        return {name: getattr(self, name) if value is UNSET else limit_value(name, value)
                for name, value in passed.items()}

    def text(self):
        return ' '.join(f'{name}={"all" if getattr(self, name) is None else getattr(self, name)}' for name in LIMIT_NAMES)


ALL = Limits()


def shown(value):
    """A resolved limit as a view() argument again: None becomes 'all'."""
    return 'all' if value is None else value


class _Missing:
    def __repr__(self):
        return '<missing>'


MISSING = _Missing()
_TOKEN = re.compile(r'([^.\[\]]+)|\[(-?\d*)\]')


def is_viewable(value):
    return hasattr(value, 'summary') and hasattr(value, 'view_tree')


def parse(path):
    """'body.items[].id' -> ['body', 'items', [], 'id']; [] maps over a list, [n] takes an element."""
    tokens, position = [], 0
    for match in _TOKEN.finditer(path):
        if match.start() != position and path[position:match.start()] != '.':
            raise ValueError(f'Bad path: {path!r}')
        key, index = match.groups()
        tokens.append(key if key is not None else ([] if index == '' else int(index)))
        position = match.end()
    if not tokens or position != len(path):
        raise ValueError(f'Bad path: {path!r}')
    return tokens


def select(tree, path):
    """Value at path, a list for [] steps, or MISSING."""
    return _select(tree, parse(path) if isinstance(path, str) else path)


def _select(value, tokens):
    if not tokens:
        return value
    if is_viewable(value):
        value = value.view_tree()
    token, rest = tokens[0], tokens[1:]
    if token == []:
        return [_select(item, rest) for item in value] if isinstance(value, list) else MISSING
    if isinstance(token, int):
        if isinstance(value, list) and -len(value) <= token < len(value):
            return _select(value[token], rest)
        return MISSING
    if isinstance(value, dict) and token in value:
        return _select(value[token], rest)
    return MISSING


def plain(value):
    """JSON-ready copy; self-rendering objects become their masked trees."""
    if is_viewable(value):
        return plain(value.view_tree())
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, bytes):
        return f'<{len(value)} bytes>'
    return value


def is_scalar(value):
    return not isinstance(value, (dict, list, tuple)) and not is_viewable(value)


def scalar(value, width):
    if value is MISSING:
        return '<missing>'
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, bytes):
        return f'<{len(value)} bytes>'
    if isinstance(value, str):
        text = value.replace('\r', '\\r').replace('\n', '\\n')
        if not text:
            return '""'
        if width is not None and len(text) > width:
            return f'{text[:width]}…(+{len(text) - width}, width={width})'
        return text
    return str(value)


def levels(value):
    """Object levels below this node down to the deepest leaf, counted like depth: lists take no level."""
    if is_viewable(value):
        return levels(value.view_tree())
    if isinstance(value, dict):
        return 1 + max(map(levels, value.values()), default=0) if value else 0
    if isinstance(value, (list, tuple)):
        return max(map(levels, value), default=0)
    return 0


def remaining(count):
    """' ↓N' when a collapsed node hides N >= 2 levels; one level of plain values needs no marker."""
    return f' ↓{count}' if count >= 2 else ''


def keys_preview(keys):
    keys = list(keys)
    more = len(keys) - KEYS_SHOWN
    return '{' + ', '.join(map(str, keys[:KEYS_SHOWN])) + (f', +{more}' if more > 0 else '') + '}'


def collapsed(value):
    """One-word shape of a container: {a, b, c, +N} or [N] {keys of its first object}, plus ↓N levels hidden."""
    if is_viewable(value):
        return value.summary()
    if isinstance(value, dict):
        return keys_preview(value) + remaining(levels(value)) if value else '{}'
    if isinstance(value, (list, tuple)):
        first = next((v for v in value if isinstance(v, dict) or is_viewable(v)), None)
        shape = f' {keys_preview(first)}' if isinstance(first, dict) else ''
        return f'[{len(value)}]' + shape + remaining(levels(value))
    return str(value)


def _inline_list(value, items, width):
    shown = value if items is None else value[:items]
    text = ', '.join(scalar(v, width) for v in shown)
    if len(shown) < len(value):
        text += f', +{len(value) - len(shown)} (items={items})'
    return f'[{text}]'


def _node(value, depth, o):
    """(text after 'label: ', lines below it); lines are indented relative to the label."""
    if is_viewable(value):
        if depth == 0:
            return value.summary(), []
        _, below = _node(value.view_tree(), depth, o)
        return value.summary(), below
    if isinstance(value, dict):
        if not value or depth == 0:
            return collapsed(value), []
        lines = []
        for key, child in value.items():
            text, below = _node(child, None if depth is None else depth - 1, o)
            lines.append(f'{key}: {text}' if text else f'{key}:')
            lines.extend('  ' + line for line in below)
        return '', lines
    if isinstance(value, (list, tuple)):
        value = list(value)
        if all(is_scalar(v) for v in value):
            return _inline_list(value, o['items'], o['width']), []
        if depth == 0:
            return collapsed(value), []
        shown = value if o['items'] is None else value[:o['items']]
        lines = []
        for item in shown:  # a list does not consume a level: its elements show their fields
            text, below = _node(item, depth, o)
            if text:
                lines.append('- ' + text)
                lines.extend('  ' + line for line in below)
            elif below:
                lines.append('- ' + below[0])
                lines.extend('  ' + line for line in below[1:])
            else:
                lines.append('- ' + collapsed(item))
        if len(shown) < len(value):
            lines.append(f'… +{len(value) - len(shown)} (items={o["items"]})')
        return f'[{len(value)}]', lines
    return scalar(value, o['width']), []


def cap(lines, limit):
    if limit is None or len(lines) <= limit:
        return lines
    hidden = len(lines) - limit
    return lines[:limit] + [f'… {hidden} more lines (lines={limit}); narrow the paths or raise lines']


def render(header, tree, paths, *, depth, items, width, lines, format):
    """The common view: a header line plus the selected paths, or JSON of the selected subtrees."""
    if format == 'json':
        data = plain(tree) if not paths else project(tree, paths)
        return json.dumps(data, ensure_ascii=False, indent=2)
    if format != 'text':
        raise ValueError("format is 'text' or 'json'")
    o = dict(items=items, width=width)
    selected = [(path, select(tree, path)) for path in paths]
    if selected and all(is_scalar(v) or (isinstance(v, list) and all(is_scalar(i) for i in v)) for _, v in selected):
        pairs = ' '.join(f'{p}={_node(v, 0, o)[0]}' for p, v in selected)
        return f'{header} · {pairs}' if header else pairs
    out = [header] if header else []
    for path, value in selected:
        text, below = _node(value, depth, o)
        out.append(f'{path}: {text}' if text else f'{path}:')
        out.extend('  ' + line for line in below)
    return '\n'.join(cap(out, lines))


def project(tree, paths):
    """Selected subtrees in their original nesting; absent fields are left out."""
    result = {}
    for path in paths:
        part = _project(tree, parse(path))
        if part is not MISSING:
            result = _merge(result, part)
    return result


def _project(value, tokens):
    if not tokens:
        return plain(value)
    if is_viewable(value):
        value = value.view_tree()
    token, rest = tokens[0], tokens[1:]
    if token == [] or isinstance(token, int):
        if not isinstance(value, list):
            return MISSING
        chosen = value if token == [] else value[token:token + 1 or None] if -len(value) <= token < len(value) else []
        parts = [_project(item, rest) for item in chosen]
        return [p for p in parts if p is not MISSING]
    if isinstance(value, dict) and token in value:
        inner = _project(value[token], rest)
        return MISSING if inner is MISSING else {token: inner}
    return MISSING


def _merge(left, right):
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _merge(merged[key], value) if key in merged else value
        return merged
    if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
        return [_merge(a, b) for a, b in zip(left, right)]
    return right


class ViewList(list):
    """A list whose printed form is one view per element, with the limits of the 'list' kind."""
    def __init__(self, items=(), *, limits=ALL):
        super().__init__(items)
        self.limits = limits

    def view(self, *paths, depth=UNSET, width=UNSET, lines=UNSET):
        o = self.limits.resolve(depth=depth, width=width, lines=lines)
        rendered = [item.view(*paths, depth=shown(o['depth']), width=shown(o['width'])) if hasattr(item, 'view') else str(item)
                    for item in self]
        return '\n'.join(cap('\n'.join(rendered).splitlines(), o['lines'])) if rendered else '(empty)'

    def __repr__(self):
        return self.view()

    __str__ = __repr__
