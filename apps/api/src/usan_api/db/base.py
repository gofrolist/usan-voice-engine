import enum

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class CallDirection(enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class CallStatus(enum.Enum):
    QUEUED = "queued"
    DIALING = "dialing"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    VOICEMAIL_LEFT = "voicemail_left"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    DNC_BLOCKED = "dnc_blocked"
    CANCELLED = "cancelled"


class ProfileStatus(enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AdminRole(enum.Enum):
    ADMIN = "admin"
    VIEWER = "viewer"
