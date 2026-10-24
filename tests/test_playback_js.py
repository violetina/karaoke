"""player_open._PLAYBACK_JS run against a stub page: song time, not queue time."""
import json
import shutil
import subprocess

import pytest

from karaoke.player_open import _PLAYBACK_JS

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="needs node to run the page script")


def _run(page_js: str) -> dict:
    script = page_js + "\nconst out = " + _PLAYBACK_JS + ";\nprocess.stdout.write(out);"
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(res.stdout)


VIDEO = """
const video = {currentTime: 2616.4, duration: 2673.0, ended: false, paused: false,
               readyState: 4, muted: false};
const location = {href: 'https://music.youtube.com/watch?v=x'};
"""


def test_youtube_music_reports_the_song_not_the_queue():
    # One <video> for the whole queue: 43 minutes in, the song at 1:57 of 2:55.
    state = _run(VIDEO + """
const player = {getCurrentTime: () => 117.4, getDuration: () => 174.1};
const document = {querySelector: s => s === 'video' ? video : s === '#movie_player' ? player : null};
""")
    assert state["present"] is True
    assert state["position"] == 117.4 and state["duration"] == 174.1


def test_a_page_without_a_youtube_player_uses_the_video_element():
    state = _run(VIDEO + """
const document = {querySelector: s => s === 'video' ? video : null};
""")
    assert state["position"] == 2616.4 and state["duration"] == 2673.0


def test_a_player_without_a_duration_yet_falls_back_to_the_element():
    state = _run(VIDEO + """
const player = {getCurrentTime: () => 0, getDuration: () => 0};
const document = {querySelector: s => s === 'video' ? video : s === '#movie_player' ? player : null};
""")
    assert state["duration"] == 2673.0
