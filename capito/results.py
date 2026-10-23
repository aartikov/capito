"""Results of calls and commands. Logic reads attributes; people and agents read view()."""
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import unquote

from . import view as views

REDACTED = '<redacted>'
# A query parameter inside any text: ?name=value or &name=value.
QUERY_PARAMETER = re.compile(r'(?<=[?&])([^=&#\s"\'<>]+)=([^&#\s"\'<>]*)')


def normalized_key(key):
    return re.sub(r'[-_]', '', str(key).lower())


class Secrets:
    """What view() hides: field names, words inside field names, text patterns, plus known secret values.

    keys match a field, header or query parameter name exactly; key_words match a part of the name, so
    'token' also hides 'tokensCount' — deliberately more than needed. Names ignore case, '-' and '_'.
    A project declares its policy once as a class attribute of its Client; results receive it at creation.
    """
    def __init__(self, keys=(), key_words=(), patterns=()):
        self.keys = frozenset(normalized_key(k) for k in keys)
        self.key_words = tuple(dict.fromkeys(normalized_key(w) for w in key_words))
        self.patterns = tuple(dict.fromkeys(patterns))
        self._compiled = [re.compile(p) for p in self.patterns]

    @classmethod
    def http(cls):
        """Credentials common to any HTTP API.

        Exact names follow Sentry's DEFAULT_DENYLIST (auth headers, cookies, sessions, CSRF, API keys);
        words cover tokens, passwords and secrets under any field name; patterns catch bearer tokens and JWTs in text.
        """
        return cls(keys=('authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'x-api-key', 'api_key', 'apikey',
                         'session', 'sessionid', 'csrf', 'csrftoken', 'x-csrf-token', 'x-xsrf-token'),
                   key_words=('token', 'password', 'passwd', 'secret'),
                   patterns=(r'(?i)(?<=bearer\s)[^\s"<>]+', r'\beyJ[\w-]+\.[\w-]+\.[\w-]+'))

    def extend(self, *, keys=(), key_words=(), patterns=()):
        """A stricter policy: these rules are added, nothing is removed."""
        return Secrets(tuple(self.keys) + tuple(keys), self.key_words + tuple(key_words), self.patterns + tuple(patterns))

    def hides(self, key):
        normalized = normalized_key(key)
        return normalized in self.keys or any(word in normalized for word in self.key_words)

    def text(self, value, values=()):
        for secret in sorted({s for s in values if isinstance(s, str) and s}, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        for pattern in self._compiled:
            value = pattern.sub(REDACTED, value)
        return QUERY_PARAMETER.sub(lambda m: f'{m[1]}={REDACTED}' if m[2] and self.hides(unquote(m[1])) else m[0], value)

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
        """The rules in force, one kind per line."""
        return '\n'.join([f"keys: {', '.join(sorted(self.keys))}", f"key_words: {', '.join(self.key_words)}",
                          f"patterns: {', '.join(self.patterns)}"])

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
        return statuses['request']
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
    for issue in error['issues']:
        path = ''.join(f'[{p}]' if isinstance(p, int) else f'.{p}' for p in issue.get('path', [])).lstrip('.') or '<root>'
        names = next(((key, issue[key]) for key in ('extra', 'missing') if issue.get(key)), None)
        if names:
            key, fields = names
            texts.append(f"{path} {key} {', '.join(fields[:5])}" + (f' +{len(fields) - 5}' if len(fields) > 5 else ''))
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
    if error and error.get('code'):
        message = error.get('message')
        return error['code'] + (f': {views.scalar(message, 80)}' if message else '')
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
