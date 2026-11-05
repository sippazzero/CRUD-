"""Structured logging configuration for API and background processes."""

import logging

from pythonjsonlogger.json import JsonFormatter


def configure_logging(level: str) -> None:
    """Configure JSON logs so container output remains machine-readable."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    )
    logging.basicConfig(
        level=level.upper(),
        handlers=[handler],
        force=True,
    )
