"""Oracle conformance assertion helpers for RetellAI-parity tests.

Discovered oracle component / SDK model names (Tasks 4-15 reference):
  Call:        oracle='V2CallResponse' (oneOf V2PhoneCallResponse|V2WebCallResponse)
               sdk='retell.types:CallResponse'  (also PhoneCallResponse, WebCallResponse)
  Agent:       oracle='AgentResponse'           sdk='retell.types:AgentResponse'
  Voice:       oracle='VoiceResponse'           sdk='retell.types:VoiceResponse'
  Concurrency: oracle=<not present>             sdk='retell.types:ConcurrencyRetrieveResponse'

Validator choice: openapi-schema-validator (OAS30Validator)
  - Understands OpenAPI 3.0's `nullable: true` (not JSON Schema's type: [..., "null"])
  - Uses the modern `referencing` library (no DeprecationWarning unlike RefResolver)
  - Resolves `$ref: '#/components/schemas/...'` via a Registry keyed to BASE_URI
"""

from __future__ import annotations

import importlib
import warnings
from functools import cache
from typing import Any

from openapi_schema_validator import OAS30Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT4

from tests.compat.oracle_loader import load_oracle

# Stable URI used to register the full oracle spec in the referencing Registry.
# All $ref values like '#/components/schemas/Foo' are resolved against this URI.
_BASE_URI = "oracle://retell-spec"


@cache
def _build_registry() -> Registry:
    """Build a referencing Registry with the full oracle spec registered at _BASE_URI.

    Cached so the registry is built once per process.
    """
    oracle = load_oracle()
    resource = Resource.from_contents(oracle, default_specification=DRAFT4)
    return Registry().with_resource(_BASE_URI, resource)


def assert_conforms(payload: dict[str, Any], component: str) -> None:
    """Validate *payload* against oracle ``components/schemas/<component>``.

    Raises ``jsonschema.exceptions.ValidationError`` if the payload does not
    conform to the pinned oracle schema.

    Handles:
    - OpenAPI 3.0 ``nullable: true`` (a null value for a nullable field PASSES)
    - ``$ref: '#/components/schemas/...'`` resolution via a registry
    - Warning-free validation (no deprecated RefResolver)
    """
    registry = _build_registry()
    # Use a wrapper $ref schema so the validator resolves against the full oracle
    # (passing the sub-schema directly breaks $ref resolution inside it)
    wrapper_schema: dict[str, Any] = {"$ref": f"{_BASE_URI}#/components/schemas/{component}"}
    validator = OAS30Validator(wrapper_schema, registry=registry)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # surface any unexpected deprecation warnings
        validator.validate(payload)


def assert_sdk_roundtrip(payload: dict[str, Any], model_path: str) -> None:
    """Assert that the retell SDK model parses *payload* without error.

    *model_path* format: ``'retell.types:ModelName'``
    e.g. ``'retell.types:VoiceResponse'``.

    Raises ``pydantic.ValidationError`` if the SDK model rejects the payload.
    """
    module_name, _, attr = model_path.partition(":")
    model = getattr(importlib.import_module(module_name), attr)
    model.model_validate(payload)
