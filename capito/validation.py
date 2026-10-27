"""Adapters to openapi-core; no endpoint-specific validation or schema rewriting."""
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit, parse_qsl, urljoin
from http.cookies import SimpleCookie
from jsonschema.validators import extend
from openapi_core import OpenAPI, Config, V31RequestValidator, V31ResponseValidator
from openapi_core.datatypes import RequestParameters
from openapi_core.deserializing.exceptions import DeserializeError
from openapi_core.deserializing.media_types.exceptions import MediaTypeDeserializeError
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


def request_adapter(request, route):
    url = urlsplit(request.url)
    cookies = SimpleCookie()
    cookies.load(next((v for k, v in request.headers.items() if k.lower() == 'cookie'), ''))
    return SimpleNamespace(
        host_url=f'{url.scheme}://{url.netloc}', path=url.path,
        full_url_pattern=f'{url.scheme}://{url.netloc}{route}', method=request.method.lower(),
        parameters=RequestParameters(query=MultiDict(parse_qsl(url.query, keep_blank_values=True)),
                                     header=Headers(request.headers), cookie=MultiDict((k, v.value) for k, v in cookies.items())),
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

    def _load(self):
        if not self.loaded:
            self.loaded = True
            try:
                self.contract = self.source.load()
                binary = {}
                if str(self.contract.document.get('openapi', '')).startswith('3.1'):
                    binary = dict(request_validator_cls=BinaryRequestValidator, response_validator_cls=BinaryResponseValidator)
                self.api = OpenAPI.from_dict(self.contract.document, config=Config(server_base_url=self.base_url, **binary))
            except Exception as exc:
                self.error = f'{type(exc).__name__}: {str(exc)[:500]}'

    def check(self, request, route, response=None, *, phase):
        self._load()
        if self.error:
            return outcome('unavailable', reason=self.error)
        op = self.contract.by_route.get((request.method, route))
        if op is None:
            return outcome('not_described', reason='Operation absent from contract')
        if phase == 'response' and (response is None or not response.status):
            return outcome('not_run', reason='No HTTP response')
        adapted = request_adapter(request, route)
        # The environment chooses the transport host. Validate against the same
        # operation at the contract's declared server (often a documentation mock).
        path_item = self.contract.deref(self.contract.document['paths'][route])
        servers = op.spec.get('servers', path_item.get('servers', self.contract.document.get('servers', [])))
        if servers:
            server = servers[0]
            target = server['url']
            for name, variable in server.get('variables', {}).items():
                target = target.replace('{' + name + '}', str(variable['default']))
            target = urljoin(self.base_url + '/', target).rstrip('/')
            actual_prefix = urlsplit(self.base_url).path.rstrip('/')
            suffix = adapted.path[len(actual_prefix):] if actual_prefix else adapted.path
            location = urlsplit(target)
            adapted.host_url = f'{location.scheme}://{location.netloc}'
            adapted.path = location.path.rstrip('/') + suffix
            adapted.full_url_pattern = target + route
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
