"""Results of calls and commands. Logic reads attributes; people and agents read view()."""
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import unquote

from . import view as views

REDACTED = '<redacted>'
# A name=value pair inside any text: after ? or & in an address, or opening a line, as in a form body
# (client_secret=…&grant_type=…). Not after a space: summaries such as 'alice  session=no' are pairs of capito's own.
# A name holds no ?, so a pair never swallows the address before it.
QUERY_PARAMETER = re.compile(r'(?:(?<=[?&])|^)([^=&#?\s"\'<>]+)=([^&#\s"\'<>]*)', re.MULTILINE)
# A JSON key inside any text, e.g. a JSON body that did not parse; its value is found by json_value_end().
JSON_KEY = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*')


def json_value_end(text, start):
    """Where the JSON value at text[start] ends: a string, an object or array, or a bare value; a cut-off one runs to the end."""
    depth, in_string, escaped = 0, False, False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
                if depth == 0:
                    return i + 1
        elif char == '"':
            in_string = True
        elif char in '{[':
            depth += 1
        elif char in '}]':
            if depth == 0:
                return i
            depth -= 1
            if depth == 0:
                return i + 1
        elif depth == 0 and (char == ',' or char.isspace()):
            return i
    return len(text)


def normalized_key(key):
    return re.sub(r'[-_]', '', str(key).lower())


class Secrets:
    """What view() hides: rules for keys (names of fields, headers and query parameters) and for text, plus known values.

    keys: a str is an exact key; re.compile(...) is searched in the key. Both see the key in lower case without '-'
    and '_': 'X-API-Key' is 'xapikey', and re.compile('token$') hides access_token and refreshToken, not prompt_tokens.
    A hidden key hides its whole value. values: re.compile(...) searched in any text; a match is cut out.
    A project declares its policy once as a class attribute of its Client; results receive it at creation.
    """
    def __init__(self, keys=(), values=()):
        for rule in keys:
            if not isinstance(rule, (str, re.Pattern)):
                raise TypeError(f'Secrets key rule {rule!r}: an exact key (str) or re.compile(...)')
        for rule in values:
            if not isinstance(rule, re.Pattern):
                raise TypeError(f'Secrets value rule {rule!r}: re.compile(...); a known secret value goes to result.add_secrets()')
        self.keys = tuple(dict.fromkeys(normalized_key(r) if isinstance(r, str) else r for r in keys))
        self.values = tuple(dict.fromkeys(values))
        self._exact = frozenset(r for r in self.keys if isinstance(r, str))
        self._searched = tuple(r for r in self.keys if isinstance(r, re.Pattern))

    @classmethod
    def http(cls):
        """Credentials common to any HTTP API.

        Exact keys follow Sentry's DEFAULT_DENYLIST (auth headers, cookies, sessions, CSRF, credentials);
        keys ending in token, apikey or privatekey follow GitLab's log filter (refreshToken, x-goog-api-key,
        but not prompt_tokens); password, passwd and secret anywhere in a key; bearer tokens and JWTs in text.
        """
        return cls(keys=('authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'session', 'sessionid', 'csrf',
                         'credentials', 'tokens', re.compile('token$'), re.compile('apikey$'), re.compile('privatekey$'),
                         re.compile('password'), re.compile('passwd'), re.compile('secret')),
                   values=(re.compile(r'(?i)(?<=bearer\s)[^\s"<>]+'), re.compile(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+')))

    def extend(self, *, keys=(), values=()):
        """A stricter policy: these rules are added, nothing is removed."""
        return Secrets(self.keys + tuple(keys), self.values + tuple(values))

    def hides(self, key):
        key = normalized_key(key)
        return key in self._exact or any(rule.search(key) for rule in self._searched)

    def text(self, value, values=()):
        # A known value is hidden as a whole token: a test key '1' must not turn status 401 into 40<redacted>.
        for secret in sorted({s for s in values if isinstance(s, str) and s}, key=len, reverse=True):
            value = re.sub(rf'(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])', REDACTED, value)
        for rule in self.values:
            value = rule.sub(REDACTED, value)
        value = self._json_pairs(value)
        return QUERY_PARAMETER.sub(lambda m: f'{m[1]}={REDACTED}' if m[2] and self.hides(unquote(m[1])) else m[0], value)

    def _json_pairs(self, text):
        """Text with the value of every hidden "key": value pair cut out, as in a parsed body."""
        parts, position = [], 0
        while found := JSON_KEY.search(text, position):
            parts.append(text[position:found.end()])
            position = found.end()
            if self.hides(found[1]) and position < len(text):
                parts.append(REDACTED)
                position = json_value_end(text, position)
        return ''.join(parts) + text[position:]

    def redact(self, value, values=(), key=''):
        """A masked copy; objects that render themselves are left to their own policy."""
        if key and self.hides(key):
            return REDACTED if value is not None else None
        if views.is_viewable(value):
            return value
        if isinstance(value, Mapping):
            return {k: self.redact(v, values, str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v, values) for v in value]
        if isinstance(value, bytes):
            return f'<{len(value)} bytes>'
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, str):
            return self.text(value, values)
        return value

    def view(self):
        """The rules in force: exact keys as they are, regular expressions between slashes."""
        shown = lambda rules: ', '.join(f'/{r.pattern}/' if isinstance(r, re.Pattern) else r for r in rules)
        return f'keys: {shown(self.keys)}\nvalues: {shown(self.values)}'

    def __repr__(self):
        return self.view()

    __str__ = __repr__


def thaw(value):
    """A plain mutable copy of frozen record data: dicts and lists again."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def strings(value):
    """Every string inside a value: secrets to hide when the value is a session or credential."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in strings(v)]
    return []


class Viewable:
    """Shared view(): print(x) is x.view() with the [view] limits configured for its kind."""
    def __init__(self, policy, secrets=(), *, limits=views.ALL):
        if not isinstance(policy, Secrets):
            raise TypeError('policy must be a Secrets instance; take it from client.core.secrets')
        self._policy = policy
        self._limits = limits
        self._secrets = {s for s in secrets if isinstance(s, str) and s}

    def add_secrets(self, *values):
        """Hide these string values wherever they appear in view()."""
        self._secrets.update(v for v in values if isinstance(v, str) and v)

    def secret_values(self):
        """Values hidden by this object; pass them to add_secrets() of an object that embeds it."""
        return tuple(self._secrets)

    def _mask(self, value):
        return self._policy.redact(value, self._secrets)

    def view_tree(self):
        return self._mask(self._tree())

    def summary(self):
        return self._policy.text(self._summary(), self._secrets)

    def view(self, *paths, depth=views.UNSET, items=views.UNSET, width=views.UNSET, lines=views.UNSET, format='text'):
        """Header line, plus selected paths expanded `depth` levels.

        A limit not passed comes from the [view] configuration of this kind of object; 'all' lifts it.
        """
        limits = self._limits.resolve(depth=depth, items=items, width=width, lines=lines)
        return views.render(self.summary(), self.view_tree(), paths, format=format, **limits)

    def __repr__(self):
        return self.view()

    __str__ = __repr__


def validation_line(phases):
    """'valid', or the phases that are not valid with their issues, e.g. 'response invalid: items[0] extra id, +6'."""
    statuses = {phase: (phases.get(phase) or {}).get('status') for phase in ('request', 'response')}
    if len(set(statuses.values())) == 1 and statuses['request'] != 'invalid':
        reason = (phases.get('request') or phases.get('response') or {}).get('reason')
        return statuses['request'] + (f' ({reason})' if reason else '')
    parts = []
    for phase, status in statuses.items():
        details = phases.get(phase) or {}
        if status == 'invalid':
            parts.append(f'{phase} invalid: ' + '; '.join(issue_text(e) for e in details.get('errors', [])))
        elif status != 'valid':
            parts.append(f'{phase} {status}' + (f" ({details['reason']})" if details.get('reason') else ''))
    return ' · '.join(parts)


def issue_text(error):
    if 'issues' not in error:
        return error.get('message', error.get('type', 'error'))
    texts = []
    # A parameter or security error starts its path from where it was found: 'query.limit', 'security'.
    base = '.'.join(str(part) for part in (error.get('in'), error.get('name')) if part)
    for issue in error['issues']:
        path = (base + ''.join(f'[{p}]' if isinstance(p, int) else f'.{p}' for p in issue.get('path', []))).lstrip('.') or '<root>'
        names = next(((key, issue[key]) for key in ('extra', 'missing') if issue.get(key)), None)
        if names:
            key, fields = names
            joint = ' or ' if issue.get('rule') == 'security' else ', '
            texts.append(f"{path} {key} {joint.join(repr(str(f)) for f in fields[:5])}" + (f' +{len(fields) - 5}' if len(fields) > 5 else ''))
        else:
            texts.append(f"{path} {issue.get('rule')}")
    return '; '.join(texts)


class Result(Viewable):
    """One HTTP call (or a local outcome without a response). All fields are plain attributes."""
    FIELDS = ('success', 'status', 'error', 'errors', 'method', 'route', 'duration', 'body', 'headers',
              'request', 'validation', 'attempts', 'request_id', 'started_at', 'finished_at', 'reused_concurrent_refresh')

    def __init__(self, *, policy, secrets=(), limits=views.ALL, **fields):
        super().__init__(policy, secrets, limits=limits)
        unknown = set(fields) - set(self.FIELDS)
        if unknown:
            raise TypeError(f'Unknown Result fields: {sorted(unknown)}')
        self.success, self.status, self.error, self.errors = False, None, None, []
        self.method = self.route = self.duration = self.body = self.request = self.validation = None
        self.headers, self.attempts = {}, []
        self.request_id = self.started_at = self.finished_at = self.reused_concurrent_refresh = None
        for key, value in fields.items():
            setattr(self, key, value)
        # Saved account behind the final HTTP attempt; set by Client.call, never shown.
        self.sent_account = None

    def _tree(self):
        tree = {key: getattr(self, key) for key in self.FIELDS if key not in ('validation', 'reused_concurrent_refresh')}
        tree['validation'] = None if self.validation is None else {
            name: validation_line(phases) for name, phases in self.validation.items()}
        if self.reused_concurrent_refresh is not None:
            tree['reused_concurrent_refresh'] = self.reused_concurrent_refresh
        return tree

    def _summary(self):
        parts = ['—' if not self.status else str(self.status)]  # 0: the transport got no response
        if self.method:
            parts[0] += f' {self.method} {self.route}'
        if self.duration is not None:
            parts.append(f'{self.duration:.2f}s')
        if not self.success:
            parts.append(error_text(self.error, self.errors))
        if self.reused_concurrent_refresh:
            parts.append('reused concurrent refresh')
        for name, phases in (self.validation or {}).items():
            statuses = [(phases.get(p) or {}).get('status') for p in ('request', 'response')]
            parts.append(f'{name}: {statuses[0]}' if statuses[0] == statuses[1] else f'{name}: req={statuses[0]} resp={statuses[1]}')
        if self.body is not None:
            body = self._mask(self.body)
            if isinstance(body, (dict, list)):
                parts.append('body ' + views.collapsed(body))
            else:
                parts.append('body ' + views.scalar(body, 40))
        return ' · '.join(p for p in parts if p)


def error_text(error, errors):
    if error and error.get('code') not in (None, ''):  # a code is any value of the API's format: 'Forbidden', 404, 0
        message = error.get('message')
        return views.scalar(error['code'], 80) + (f': {views.scalar(message, 80)}' if message else '')
    if error and error.get('message'):
        return views.scalar(error['message'], 80)
    return views.scalar(errors[0], 80) if errors else 'failed'


def error_from(errors, code=None, message=None):
    """Normalized failure: the parsed server error, or the first local reason."""
    meaningful = [e for e in errors if not str(e).startswith('Unexpected HTTP status')]
    return {'code': code, 'message': message or (meaningful or errors or [None])[0]}


class CompoundResult(Viewable):
    """A command of several dependent requests: steps keep every response, data keeps the IDs obtained."""
    def __init__(self, name, *, policy, limits=views.ALL):
        super().__init__(policy, limits=limits)
        self.name = name
        self.success, self.errors, self.steps, self.data = True, [], {}, {}

    def _tree(self):
        return {'success': self.success, 'errors': self.errors, 'data': self.data, 'steps': self.steps}

    def _summary(self):
        parts = [('OK ' if self.success else 'FAILED ') + self.name]
        if not self.success:
            failed_step = next((name for name, step in reversed(self.steps.items()) if not step.success), None)
            if failed_step:
                step = self.steps[failed_step]
                parts.append(f'{failed_step}: ' + ' '.join(filter(None, [None if step.status is None else str(step.status),
                                                                           error_text(step.error, step.errors)])))
            else:
                parts.append(views.scalar(self.errors[-1], 80) if self.errors else 'failed')
        parts.extend(steps_validation(self.steps))
        if self.data:
            data = self._mask(self.data)
            parts.append('data: ' + ', '.join(f'{k}={views._node(v, 0, dict(items=None, width=None))[0]}' for k, v in data.items()))
        return ' · '.join(parts)


class Account(Viewable):
    """A local account without credentials."""
    def __init__(self, record, *, policy, limits=views.ALL, summary_identity=()):
        super().__init__(policy, strings(record.session), limits=limits)
        self.id, self.alias = record.id, record.alias
        self.identity, self.settings = thaw(record.identity), thaw(record.settings)
        self.has_session = bool(record.session)
        self._summary_identity = tuple(summary_identity)

    def _tree(self):
        return {'id': self.id, 'alias': self.alias, 'identity': self.identity, 'settings': self.settings,
                'has_session': self.has_session}

    def _summary(self):
        known = [f'{key}={self.identity[key]}' for key in self._summary_identity if self.identity.get(key) is not None]
        return '  '.join([self.alias, *known, 'session=' + ('yes' if self.has_session else 'no')])

    def __eq__(self, other):
        return isinstance(other, Account) and self._tree() == other._tree()

    __hash__ = None


def validated_steps(steps, prefix=''):
    """(step name, validation) for every step that was validated; nested commands as `outer.inner`."""
    for name, step in steps.items():
        if isinstance(step, CompoundResult):
            yield from validated_steps(step.steps, f'{prefix}{name}.')
        elif getattr(step, 'validation', None):
            yield prefix + name, step.validation


def steps_validation(steps):
    """Per contract: the status every validated step shares, or only the steps that are not valid.

    'backend_api: valid', 'backend_api: upload resp=invalid, publish invalid'; details stay in
    view('steps.<name>.validation').
    """
    by_contract = {}
    for name, validation in validated_steps(steps):
        for contract, phases in validation.items():
            statuses = tuple((phases.get(phase) or {}).get('status') for phase in ('request', 'response'))
            by_contract.setdefault(contract, []).append((name, statuses))
    parts = []
    for contract, rows in by_contract.items():
        shared = {status for _, statuses in rows for status in statuses}
        if len(shared) == 1:
            parts.append(f'{contract}: {shared.pop()}')
            continue
        deviating = []
        for name, (request, response) in rows:
            if request == response:
                if request != 'valid':
                    deviating.append(f'{name} {request}')
            else:
                phases = (('req', request), ('resp', response))
                deviating.append(name + ' ' + ' '.join(f'{phase}={status}' for phase, status in phases if status != 'valid'))
        parts.append(f"{contract}: {', '.join(deviating)}")
    return parts


def add_step(result, name, step):
    result.steps[name] = step
    result.add_secrets(*step.secret_values())
    if not step.success:
        result.success = False
        result.errors.append(f'Step failed: {name}')
    return step.success


def body_value(result, key):
    """A field of a JSON object body, or None when the body is not an object."""
    return result.body.get(key) if isinstance(result.body, dict) else None


def failed(result, message):
    result.success = False
    result.errors.append(message)
    return result


def utcnow():
    return datetime.now(timezone.utc).isoformat()
