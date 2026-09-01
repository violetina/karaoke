"""Central logging configuration for karaoke."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .config import settings

def setup_logging():
    log_dir = Path(settings.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "karaoke.log"
    
    logger = logging.getLogger("karaoke")
    if logger.hasHandlers():
        return logger
        
    logger.setLevel(logging.DEBUG)
    
    handler = RotatingFileHandler(
        log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger

log = setup_logging()