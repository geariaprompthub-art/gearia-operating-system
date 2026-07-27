import logging

from app.core.structured_logging import SafeStructuredLogger, StructuredLogFormatter


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def get_structured_logger(name: str) -> SafeStructuredLogger:
    """Create a stable named JSON logger without changing legacy handlers."""

    logger = logging.getLogger(f"{name}.structured")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(StructuredLogFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    return SafeStructuredLogger(logger)
