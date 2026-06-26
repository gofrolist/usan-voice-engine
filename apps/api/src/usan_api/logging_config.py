import inspect
import json
import logging
import os
import re
import sys
import traceback
from typing import Any, Literal

from loguru import logger

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Redact the phone-number segment of get/update/delete-phone-number paths from access logs.
# The oracle forces {phone_number} as a literal path param (cannot be opaque-encoded like
# call_id). uvicorn URL-encodes the path via urllib.parse.quote, so a '+' sign becomes
# '%2B' and punctuated forms like +1-949-555-1234 survive as-is or percent-encoded.
# Matching everything up to the next delimiter (whitespace / " / ?) handles all variants:
# literal E.164, %2B-encoded, and punctuated/hyphenated forms.
_PHONE_PATH_RE = re.compile(r'(/(?:get|update|delete)-phone-number/)[^\s/"?]+')


def _mask_phi_path(message: str) -> str:
    return _PHONE_PATH_RE.sub(r"\1[redacted]", message)


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


def _json_sink(message: Any) -> None:
    """loguru sink writing one Cloud Logging JSON line.

    Defined (vs an inline lambda) so it returns ``None`` rather than
    ``sys.stdout.write``'s ``int`` — satisfying loguru's ``Callable[[Message], None]``
    sink type (mypy).
    """
    sys.stdout.write(_gcp_serialize(message.record) + "\n")


class _InterceptHandler(logging.Handler):
    """Route stdlib logging (uvicorn) into loguru so every line shares one JSON format.

    Drops uvicorn.access records for /health — the uptime check + Caddy probes would
    otherwise spam the logs with no operational value.
    """

    def emit(self, record: logging.LogRecord) -> None:
        message = _mask_phi_path(record.getMessage())
        if record.name == "uvicorn.access" and "/health" in message:
            return
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk back past the logging machinery so loguru reports the real caller.
        frame, depth = inspect.currentframe(), 0
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, message)


def _intercept_uvicorn() -> None:
    """Redirect uvicorn's loggers through loguru (uvicorn installs its own handlers)."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [_InterceptHandler()]
        lg.propagate = False


def configure_logging(level: LogLevel = "INFO") -> None:
    """Configure loguru to log to stdout.

    ``LOG_FORMAT=json`` emits one Cloud Logging JSON object per line (for the Ops Agent
    to ingest in prod); anything else keeps the human-readable format for local dev.
    """
    logger.remove()
    if os.environ.get("LOG_FORMAT", "").lower() == "json":
        logger.add(
            _json_sink,
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
    # Funnel uvicorn's access/error logs through loguru (consistent JSON; /health dropped).
    _intercept_uvicorn()
