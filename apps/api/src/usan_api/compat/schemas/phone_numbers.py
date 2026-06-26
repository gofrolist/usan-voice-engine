"""RetellAI-compat phone-number request/response schemas + serializer.

Oracle field-name traps preserved: import uses sip_trunk_auth_*, update uses auth_*; the
response NEVER carries auth_password. sms-agent fields + inbound_sms_webhook_url are
update/response only. nickname is plain-optional on import (oracle: not nullable) and
nullable on update. ignore_e164_validation is a StrictBool (string "true"/"false" invalid).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, field_validator

from usan_api.db.models import PhoneNumber
from usan_api.ssrf_guard import validate_webhook_url

_TRANSPORTS = frozenset({"TLS", "TCP", "UDP"})


def _check_webhook(v: str) -> str:
    validate_webhook_url(v)  # raises ValueError -> 422 via the global handler
    return v


def _check_transport(v: str) -> str:
    upper = v.upper()
    if upper not in _TRANSPORTS:
        raise ValueError("transport must be one of TLS, TCP, UDP")
    return upper


# Reusable, shared across models. AfterValidator runs only on the str branch of `… | None`,
# so the helpers never see None (no None-guard needed) and the wiring is unambiguous in v2.
WebhookUrl = Annotated[str, AfterValidator(_check_webhook)]
Transport = Annotated[str, AfterValidator(_check_transport)]


class AgentWeight(BaseModel):
    model_config = ConfigDict(extra="ignore")
    agent_id: str = Field(min_length=1)
    weight: float = Field(gt=0, le=1)
    agent_version: int | str | None = None

    @field_validator("agent_version")
    @classmethod
    def _nonneg(cls, v: int | str | None) -> int | str | None:
        if isinstance(v, int) and v < 0:
            raise ValueError("agent_version must be >= 0")
        return v


class SipOutboundTrunkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    termination_uri: str | None = None
    auth_username: str | None = None
    transport: str | None = None
    # NO auth_password — write-only, never echoed.


class PhoneNumberResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_number: str
    phone_number_type: str
    last_modification_timestamp: int
    phone_number_pretty: str | None = None
    area_code: int | None = None
    nickname: str | None = None
    inbound_webhook_url: str | None = None
    inbound_sms_webhook_url: str | None = None
    allowed_inbound_country_list: list[str] | None = None
    allowed_outbound_country_list: list[str] | None = None
    inbound_agents: list[AgentWeight] | None = None
    outbound_agents: list[AgentWeight] | None = None
    inbound_sms_agents: list[AgentWeight] | None = None
    outbound_sms_agents: list[AgentWeight] | None = None
    sip_outbound_trunk_config: SipOutboundTrunkConfig | None = None
    fallback_number: str | None = None


class ImportPhoneNumberRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    phone_number: str = Field(min_length=1)
    termination_uri: str
    ignore_e164_validation: StrictBool = True
    sip_trunk_auth_username: str | None = None
    sip_trunk_auth_password: str | None = None
    inbound_agents: list[AgentWeight] | None = None
    outbound_agents: list[AgentWeight] | None = None
    nickname: str | None = None
    inbound_webhook_url: WebhookUrl | None = None
    allowed_inbound_country_list: list[str] | None = None
    allowed_outbound_country_list: list[str] | None = None
    transport: Transport | None = None


class UpdatePhoneNumberRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    inbound_agents: list[AgentWeight] | None = None
    outbound_agents: list[AgentWeight] | None = None
    inbound_sms_agents: list[AgentWeight] | None = None
    outbound_sms_agents: list[AgentWeight] | None = None
    nickname: str | None = None
    inbound_webhook_url: WebhookUrl | None = None
    inbound_sms_webhook_url: WebhookUrl | None = None
    allowed_inbound_country_list: list[str] | None = None
    allowed_outbound_country_list: list[str] | None = None
    termination_uri: str | None = None
    auth_username: str | None = None
    auth_password: str | None = None
    transport: Transport | None = None
    fallback_number: str | None = None


def _agents(raw: list[dict[str, Any]] | None) -> list[AgentWeight] | None:
    return [AgentWeight(**a) for a in raw] if raw else None


def serialize_phone_number(pn: PhoneNumber) -> PhoneNumberResponse:
    trunk = None
    if pn.termination_uri or pn.sip_auth_username or pn.transport:
        trunk = SipOutboundTrunkConfig(
            termination_uri=pn.termination_uri,
            auth_username=pn.sip_auth_username,
            transport=pn.transport or "TCP",
        )
    return PhoneNumberResponse(
        phone_number=pn.phone_e164,
        phone_number_type=pn.phone_number_type,
        last_modification_timestamp=int(pn.updated_at.timestamp() * 1000),
        # phone_number_pretty + area_code reserved (always null in Phase 2; omitted by exclude_none)
        nickname=pn.nickname,
        inbound_webhook_url=pn.inbound_webhook_url,
        inbound_sms_webhook_url=pn.inbound_sms_webhook_url,
        allowed_inbound_country_list=pn.allowed_inbound_country_list,
        allowed_outbound_country_list=pn.allowed_outbound_country_list,
        inbound_agents=_agents(pn.inbound_agents),
        outbound_agents=_agents(pn.outbound_agents),
        inbound_sms_agents=_agents(pn.inbound_sms_agents),
        outbound_sms_agents=_agents(pn.outbound_sms_agents),
        sip_outbound_trunk_config=trunk,
        fallback_number=pn.fallback_number,
    )
