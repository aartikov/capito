"""Adapters to openapi-core; no endpoint-specific validation or schema rewriting."""
import json
import re
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qsl, urljoin
from jsonschema.validators import extend
from openapi_core import OpenAPI, Config, V31RequestValidator, V31ResponseValidator
from openapi_core.datatypes import RequestParameters
from openapi_core.deserializing.exceptions import DeserializeError
from openapi_core.deserializing.media_types.exceptions import MediaTypeDeserializeError
from openapi_core.deserializing.media_types.util import json_loads
from openapi_core.templating.security.exceptions import SecurityNotFound
from openapi_core.validation.request.exceptions import MissingParameterError, ParameterValidationError, SecurityValidationError
from openapi_core.validation.schemas.factories import DialectSchemaValidatorsFactory
from openapi_schema_validator import OAS30ReadValidator, OAS31Validator
from openapi_schema_validator.validators import OAS31_BASE_DIALECT_ID
from werkzeug.datastructures import Headers, MultiDict

from .transport import first


# openapi-core decodes a binary form part or body to bytes. OAS 3.0 validation counts bytes as a string
# with format: binary; OAS 3.1 validation lacks that rule, so every file upload looked invalid. The rule
# is added back for 3.1; remove it once openapi-schema-validator has it.
def binary_type(validator, types, instance, schema):
    if isinstance(instance, bytes) and schema.get('format') == 'binary' and 'string' in ([types] if isinstance(types, str) else types):
        return
    yield from OAS31Validator.VALIDATORS['type'](validator, types, instance, schema)


OAS31BinaryValidator = extend(OAS31Validator, {'type': binary_type})


class BinaryValidatorsFactory(DialectSchemaValidatorsFactory):
    """openapi-core's OAS 3.1 factory with the binary rule; schemas of other dialects keep their validator."""
    def get_validator_cls(self, spec, schema):
        found = super().get_validator_cls(spec, schema)
        return OAS31BinaryValidator if found is OAS31Validator else found


binary_validators = BinaryValidatorsFactory(OAS31Validator, OAS31_BASE_DIALECT_ID, format_checker=OAS30ReadValidator.FORMAT_CHECKER)


class BinaryRequestValidator(V31RequestValidator):
    schema_validators_factory = binary_validators


class BinaryResponseValidator(V31ResponseValidator):
    schema_validators_factory = binary_validators



def outcome(status, errors=None, reason=None):
    result = {'status': status, 'errors': errors or []}
    if reason:
        result['reason'] = reason
    return result


def issue(error):
    """Schema path and rule; for unexpected or absent fields, their names (never their values)."""
    found = {'path': list(error.absolute_path), 'rule': error.validator, 'expected': error.validator_value}
    instance, schema = error.instance, error.schema if isinstance(error.schema, dict) else {}
    if error.validator == 'additionalProperties' and isinstance(instance, dict):
        declared, patterns = schema.get('properties', {}), schema.get('patternProperties', {})
        found['extra'] = [k for k in instance if k not in declared and not any(re.search(p, k) for p in patterns)]
    elif error.validator == 'required' and isinstance(instance, dict) and isinstance(error.validator_value, list):
        found['missing'] = [k for k in error.validator_value if k not in instance]
    return found


def causes(error):
    while error is not None:
        yield error
        error = error.__cause__


def undecodable(error):
    """Why a body or value could not be decoded, without the value: openapi-core puts the whole body into its message."""
    if isinstance(error, MediaTypeDeserializeError):
        reason = error.__context__  # raised without `from`: the parser's own error is the context
        position = f': {reason.msg} at line {reason.lineno} column {reason.colno}' if isinstance(reason, json.JSONDecodeError) else ''
        return f'Not valid {error.mimetype}{position}'
    return type(error).__name__


def error_details(error):
    # Library exception strings can embed an entire image or response, and with it secrets. Keep useful
    # schema paths/rules; values of bodies and parameters never get into the validation report.
    found = {'type': type(error).__name__}
    if isinstance(error, ParameterValidationError):
        found['in'] = error.location
        if isinstance(error, MissingParameterError):
            return found | {'issues': [{'path': [], 'rule': 'required', 'expected': [error.name], 'missing': [error.name]}]}
        found['name'] = error.name
    if isinstance(error, SecurityValidationError):
        missing = next((c for c in causes(error) if isinstance(c, SecurityNotFound)), None)
        if missing is not None:
            names = [' + '.join(schemes) for schemes in missing.schemes]
            return found | {'in': 'security', 'issues': [{'path': [], 'rule': 'security', 'expected': missing.schemes, 'missing': names}]}
    for cause in causes(error):
        if getattr(cause, 'schema_errors', None):
            issues = []
            for e in cause.schema_errors:  # jsonschema repeats a 'required' error for every absent field
                if (found_issue := issue(e)) not in issues:
                    issues.append(found_issue)
            return found | {'issues': issues}
        if isinstance(cause, DeserializeError):
            return found | {'message': undecodable(cause)}
    return found | {'message': str(error)[:500]}


def content_types(value):
    """Every media type named in a `content` map anywhere in the document."""
    if isinstance(value, dict):
        if isinstance(value.get('content'), dict):
            yield from value['content']
        for child in value.values():
            yield from content_types(child)
    elif isinstance(value, list):
        for child in value:
            yield from content_types(child)


def request_adapter(request, route, server):
    """The request as openapi-core reads it, with the operation already known.

    The route and its path values come from call() through openapi-core's path_pattern protocol, so the operation
    is never looked up by the transport URL, and a path value is checked as given, not URL-encoded. The route sits
    under the contract's server: the environment's base_url plays no part in finding the operation.
    """
    url, location = urlsplit(request.url), urlsplit(server)
    # name=value pairs split as servers do; SimpleCookie drops the whole header at a character it dislikes.
    cookie = next((v for k, v in request.headers.items() if k.lower() == 'cookie'), '')
    cookies = [tuple(part.strip() for part in pair.split('=', 1)) for pair in cookie.split(';') if '=' in pair]
    return SimpleNamespace(
        host_url=f'{location.scheme}://{location.netloc}', path=url.path,
        path_pattern=location.path.rstrip('/') + route, method=request.method.lower(),
        parameters=RequestParameters(query=MultiDict(parse_qsl(url.query, keep_blank_values=True)),
                                     header=Headers(request.headers), cookie=MultiDict(cookies), path=dict(request.path or {})),
        content_type=(request.content_type or '').lower(), body=request.body)


def response_adapter(response):
    return SimpleNamespace(status_code=response.status, headers=Headers(response.headers),
                           content_type=(first(response.headers.get('content-type')) or '').lower(),
                           data=response.content or None)


class Validator:
    """Checks requests and responses against one contract; an unreadable contract makes results 'unavailable'."""
    def __init__(self, source, base_url):
        self.source = source
        self.base_url = base_url
        self.loaded = False
        self.error = None
        self._guard = threading.Lock()

    def _load(self):
        # Requests run in parallel: the first ones wait until the validator is built or its error is known.
        with self._guard:
            if self.loaded:
                return
            try:
                self.contract = self.source.load()
                binary = {}
                if str(self.contract.document.get('openapi', '')).startswith('3.1'):
                    binary = dict(request_validator_cls=BinaryRequestValidator, response_validator_cls=BinaryResponseValidator)
                # openapi-core reads only application/json and application/vnd.api+json as JSON; other +json types
                # were checked as raw bytes, so every body of theirs was invalid.
                json_types = {media: json_loads for media in content_types(self.contract.document) if media.endswith('+json')}
                self.api = OpenAPI.from_dict(self.contract.document, config=Config(
                    server_base_url=self.base_url, extra_media_type_deserializers=json_types, **binary))
            except Exception as exc:
                self.error = f'{type(exc).__name__}: {str(exc)[:500]}'
            self.loaded = True

    def _server(self, op, route):
        """The first server the contract gives the operation, '/' when it gives none, as OpenAPI defines."""
        path_item = self.contract.deref(self.contract.document['paths'][route])
        servers = op.spec.get('servers', path_item.get('servers', self.contract.document.get('servers'))) or [{'url': '/'}]
        target = servers[0]['url']
        for name, variable in servers[0].get('variables', {}).items():
            target = target.replace('{' + name + '}', str(variable['default']))
        return urljoin(self.base_url + '/', target)

    def check(self, request, route, response=None, *, phase):
        self._load()
        if self.error:
            return outcome('unavailable', reason=self.error)
        op = self.contract.by_route.get((request.method, route))
        if op is None:
            return outcome('not_described', reason='Operation absent from contract')
        if phase == 'response' and (response is None or not response.status):
            return outcome('not_run', reason='No HTTP response')
        adapted = request_adapter(request, route, self._server(op, route))
        try:
            errors = list(self.api.iter_request_errors(adapted)) if phase == 'request' else list(self.api.iter_response_errors(adapted, response_adapter(response)))
            if errors:
                return outcome('invalid', [error_details(e) for e in errors])
            # A body without any schema was not checked; never report it as conformant.
            if phase == 'request':
                described = self.contract.deref(op.spec.get('requestBody', {}))
                has_body = request.body is not None
            else:
                responses = op.spec.get('responses', {})
                described = self.contract.deref(responses.get(str(response.status), responses.get(f'{response.status // 100}XX', responses.get('default', {}))))
                has_body = bool(response.content)
            content = described.get('content', {})
            if has_body and not any('schema' in media for media in content.values()):
                return outcome('not_described', reason='Body schema absent')
            return outcome('valid')
        except Exception as exc:
            return outcome('unavailable', reason=f'{type(exc).__name__}: {str(exc)[:500]}')
