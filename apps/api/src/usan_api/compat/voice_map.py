"""Voice alias map between RetellAI ``voice_id`` and the curated Cartesia catalog (T032).

RetellAI agents reference voices by an opaque ``voice_id`` (e.g. ``retell-Sarah``). This
module aliases that external id onto the native ``cartesia_voice_id`` of the curated
``VOICE_CATALOG`` and back, so an agent create/update can be expressed in RetellAI terms
while the engine speaks the catalog's real Cartesia ids. An unmapped/unhosted ``voice_id``
raises a documented ``CompatError(422)`` (FR-033) rather than an opaque validation error.

PENDING-FREEZE (oracle): the CRM's *real* historical RetellAI ``voice_id`` strings (e.g.
``11labs-Adrian``) are not known here; they are pinned against the captured CRM usage and
added as explicit aliases before the contract freezes. Until then the engine accepts its
own ``retell-<Name>`` aliases and raw curated Cartesia ids, and 422s anything else.
"""

from __future__ import annotations

import re

from usan_api.compat.errors import CompatError
from usan_api.schemas.voice_catalog import VOICE_CATALOG

_RETELL_PREFIX = "retell-"


def _short_alias(name: str) -> str:
    # "Sarah - Mindful Woman" -> "retell-Sarah"; matches RetellAI's short voice_id shape.
    first = name.split(" - ")[0].split()[0]
    return _RETELL_PREFIX + first


def _full_alias(name: str) -> str:
    # Collision fallback: a stable slug of the whole display name.
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return _RETELL_PREFIX + slug


def _build_maps() -> tuple[dict[str, str], dict[str, str]]:
    retell_to_cartesia: dict[str, str] = {}
    cartesia_to_retell: dict[str, str] = {}
    for spec in VOICE_CATALOG:
        alias = _short_alias(spec.name)
        if alias in retell_to_cartesia:  # two catalog names share a first word
            alias = _full_alias(spec.name)
        retell_to_cartesia[alias] = spec.cartesia_voice_id
        cartesia_to_retell[spec.cartesia_voice_id] = alias
    return retell_to_cartesia, cartesia_to_retell


_RETELL_TO_CARTESIA, _CARTESIA_TO_RETELL = _build_maps()


def resolve_voice_id(voice_id: str) -> str:
    """RetellAI ``voice_id`` -> curated ``cartesia_voice_id``. Accepts our ``retell-<Name>``
    aliases AND a raw curated Cartesia id (passthrough). Unhosted -> ``CompatError(422)``."""
    if voice_id in _CARTESIA_TO_RETELL:  # a raw curated cartesia id is hosted as-is
        return voice_id
    cartesia = _RETELL_TO_CARTESIA.get(voice_id)
    if cartesia is None:
        raise CompatError(
            422,
            f"voice '{voice_id}' is not a hosted voice; choose a voice from the hosted catalog",
        )
    return cartesia


def to_retell_voice_id(cartesia_voice_id: str | None) -> str | None:
    """Reverse alias for echoing ``voice_id`` in responses. Unknown/None ids pass through
    unchanged (a profile may carry a non-catalog or absent voice — never raise on read)."""
    if cartesia_voice_id is None:
        return None
    return _CARTESIA_TO_RETELL.get(cartesia_voice_id, cartesia_voice_id)
