import uuid

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    """Base for in-call tool requests: the call this tool action belongs to.

    The handler asserts this matches the JWT `call_id` claim and derives elder_id
    from the call — elder_id is never accepted from the request.
    """

    call_id: uuid.UUID


class LogWellnessRequest(ToolCallRequest):
    mood: int | None = Field(default=None, ge=1, le=5)
    pain_level: int | None = Field(default=None, ge=0, le=10)
    notes: str | None = Field(default=None, max_length=2000)


class LoggedResponse(BaseModel):
    id: int
