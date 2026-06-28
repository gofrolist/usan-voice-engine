"""RetellAI knowledge-base compat schemas (Phase 5). Responses omit None via the route's
response_model_exclude_none=True. Parsed* are the multipart-decoded request DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict


class KbTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    text: str


class KbTextSource(BaseModel):
    type: Literal["text"] = "text"
    source_id: str
    title: str
    content_url: str


class KnowledgeBaseResponse(BaseModel):
    knowledge_base_id: str
    knowledge_base_name: str
    status: str
    knowledge_base_sources: list[KbTextSource] | None = None
    enable_auto_refresh: bool | None = None
    max_chunk_size: int | None = None
    min_chunk_size: int | None = None


@dataclass
class ParsedKbCreate:
    name: str
    texts: list[KbTextInput] = field(default_factory=list)
    has_files: bool = False
    has_urls: bool = False
    enable_auto_refresh: bool = False
    max_chunk_size: int = 2000
    min_chunk_size: int = 400


@dataclass
class ParsedKbAddSources:
    texts: list[KbTextInput] = field(default_factory=list)
    has_files: bool = False
    has_urls: bool = False
