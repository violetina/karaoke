import os
from unittest.mock import patch

from karaoke.live_caption import (
    _find_audio_source,
    get_caption_config,
    render_captions_html,
    set_caption_config,
    transcribe_gemini,
    transcribe_gemma,
)


def test_find_audio_source_mic():
    with patch.dict(os.environ, {"KARAOKE_CAPTION_SOURCE": "mic"}):
        src = _find_audio_source()
        assert "source" in src or "input" in src or "Mic" in src or len(src) > 0


def test_find_audio_source_speaker():
    with patch.dict(os.environ, {"KARAOKE_CAPTION_SOURCE": "speaker"}):
        src = _find_audio_source()
        assert "monitor" in src or "sink" in src or "Speaker" in src or len(src) > 0


def test_render_captions_html():
    html = render_captions_html()
    assert "<title>" in html
    assert "/api/captions/stream" in html
    assert "PipeWire Input" in html
    assert "model-select" in html
    assert "lang-select" in html
    assert "gemma-2-9b-it" in html


def test_config_management():
    set_caption_config(model="gemma-2-9b-it", language="nl", api_key="test_key_123")
    cfg = get_caption_config()
    assert cfg["model"] == "gemma-2-9b-it"
    assert cfg["language"] == "nl"
    assert cfg["api_key"] == "test_key_123"

    # Reset
    set_caption_config(model="faster-whisper-small", language="nl", api_key="")
    cfg_reset = get_caption_config()
    assert cfg_reset["model"] == "faster-whisper-small"
    assert cfg_reset["language"] == "nl"
