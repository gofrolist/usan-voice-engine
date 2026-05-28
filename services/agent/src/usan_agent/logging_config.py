import sys
from typing import Literal

from loguru import logger

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def configure_logging(level: LogLevel = "INFO") -> None:
    """Configure loguru to emit structured logs to stdout."""
    logger.remove()
    logger.add(
        sys.stdout,
        level=level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"
        ),
        backtrace=True,
        diagnose=False,
        enqueue=True,
    )
