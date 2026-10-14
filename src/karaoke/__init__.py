"""karaoke-000: local karaoke + lyric semantic search."""
import os

os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

__version__ = "1.0.0"

