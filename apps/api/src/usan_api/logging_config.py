import json
import os
import sys
import traceback
from typing import Any, Literal

from loguru import logger

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# loguru level name -> Cloud Logging LogSeverity. Lets Cloud Logging colour/filter
# by severity instead of treating every line as default INFO.
_LEVEL_TO_SEVERITY = {
    "TRACE": "DEBUG",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "SUCCESS": "INFO",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "CRITICAL": "CRITICAL",
}

_TEXT_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"


def _gcp_serialize(record: Any) -> str:
    """Render a loguru record as one Cloud Logging JSON line.

    Bound ``extra`` fields (e.g. call_id, client, segments) are promoted to top-level
    jsonPayload fields so Cloud Logging indexes them; the reserved keys set below
    always win over ``extra`` so a stray bound key can't shadow severity/message.
    Local variables are never serialized (matches diagnose=False) — only the formatted
    exception text is included, never PHI from frames.
    """
    payload: dict[str, Any] = dict(record["extra"])
    payload["severity"] = _LEVEL_TO_SEVERITY.get(record["level"].name, "DEFAULT")
    payload["message"] = record["message"]
    payload["timestamp"] = record["time"].isoformat()
    payload["logger"] = f"{record['name']}:{record['function']}:{record['line']}"
    exc = record["exception"]
    if exc is not None:
        payload["exception"] = "".join(
            traceback.format_exception(exc.type, exc.value, exc.traceback)
        )
    return json.dumps(payload, default=str)


def configure_logging(level: LogLevel = "INFO") -> None:
    """Configure loguru to log to stdout.

    ``LOG_FORMAT=json`` emits one Cloud Logging JSON object per line (for the Ops Agent
    to ingest in prod); anything else keeps the human-readable format for local dev.
    """
    logger.remove()
    if os.environ.get("LOG_FORMAT", "").lower() == "json":
        logger.add(
            lambda m: sys.stdout.write(_gcp_serialize(m.record) + "\n"),
            level=level,
            backtrace=True,
            diagnose=False,  # don't leak local vars (PHI) into prod logs
            enqueue=True,
        )
    else:
        logger.add(
            sys.stdout,
            level=level,
            format=_TEXT_FORMAT,
            backtrace=True,
            diagnose=False,
            enqueue=True,
        )
