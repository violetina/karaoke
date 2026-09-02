"""Central logging configuration for karaoke."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional
from .config import settings

LOG_DIR = Path(settings.data_dir) / "logs"
LOG_FILE = LOG_DIR / "karaoke.log"
OPEN_STDOUT_LOG = LOG_DIR / "xdg-open.stdout.log"
OPEN_STDERR_LOG = LOG_DIR / "xdg-open.stderr.log"

# Marker attached to the console stream handler so we can find/replace it.
_CONSOLE_HANDLER_NAME = "karaoke-console"

# Accepted values for KARAOKE_LOG / stream_logs(level=...):
#   off | err | error   -> only warnings+errors (the "err only" mode)
#   full | debug        -> everything
#   info                 -> info and above
_LEVEL_ALIASES = {
    "off": logging.CRITICAL + 10,  # effectively silent
    "err": logging.WARNING,
    "error": logging.WARNING,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "full": logging.DEBUG,
    "debug": logging.DEBUG,
    "all": logging.DEBUG,
}


def _resolve_level(level: str) -> int:
    """Map a friendly level name to a logging level int."""
    return _LEVEL_ALIASES.get((level or "").strip().lower(), logging.WARNING)


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("karaoke")
    if logger.hasHandlers():
        _apply_env_console(logger)
        return logger

    logger.setLevel(logging.DEBUG)

    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    _apply_env_console(logger)
    return logger


def stream_logs(level: str = "err", *, stream=None) -> Optional[logging.Handler]:
    """Attach (or update) a console log handler so CLI runs show what's happening.

    ``level`` accepts friendly names: "err"/"error" (warnings+errors only),
    "info", "full"/"debug" (everything), or "off" to detach. Returns the active
    handler, or None when detached. Use for CLI tools/functions where you want
    live feedback instead of tailing the log file.
    """
    logger = logging.getLogger("karaoke")
    # Remove any existing console handler first (so calls are idempotent).
    for h in list(logger.handlers):
        if getattr(h, "name", "") == _CONSOLE_HANDLER_NAME:
            logger.removeHandler(h)
    if (level or "").strip().lower() == "off":
        return None
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.name = _CONSOLE_HANDLER_NAME
    handler.setLevel(_resolve_level(level))
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return handler


def _apply_env_console(logger: logging.Logger) -> None:
    """Honor the KARAOKE_LOG env var to stream logs on import (e.g. CLI use)."""
    env = os.environ.get("KARAOKE_LOG")
    if env:
        stream_logs(env)


log = setup_logging()