import sys

from loguru import logger


def configure_logging(level: str = "INFO") -> None:
    """Configure loguru to emit structured logs to stdout."""
    logger.remove()
    logger.add(
        sys.stdout,
        level=level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | " "{name}:{function}:{line} - {message}"
        ),
        backtrace=True,
        diagnose=False,  # don't leak local vars into prod logs
        enqueue=True,
    )
