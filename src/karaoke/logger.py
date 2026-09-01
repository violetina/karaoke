"""Central logging configuration for karaoke."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .config import settings

LOG_DIR = Path(settings.data_dir) / "logs"
LOG_FILE = LOG_DIR / "karaoke.log"
OPEN_STDOUT_LOG = LOG_DIR / "xdg-open.stdout.log"
OPEN_STDERR_LOG = LOG_DIR / "xdg-open.stderr.log"


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    logger = logging.getLogger("karaoke")
    if logger.hasHandlers():
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
    
    return logger

log = setup_logging()