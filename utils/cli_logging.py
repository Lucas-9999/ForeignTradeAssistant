"""Shared, metadata-safe logging bootstrap for standalone operator CLIs."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config


def configure_cli_logging(*, log_dir: Path | None = None) -> logging.Handler:
    """Persist CLI INFO audit events using the application's log format.

    The app configures logging at import time, while standalone commands do
    not.  This small bootstrap gives operator CLIs the same rotating UTF-8 log
    without adding duplicate handlers when a command is invoked repeatedly in
    one process.
    """
    directory = Path(log_dir or config.LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / "fta.log").resolve()
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_fta_cli_log_path", None) == path:
            return handler

    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler._fta_cli_log_path = path  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter(config.LOG_FORMAT))
    root.addHandler(handler)
    return handler
