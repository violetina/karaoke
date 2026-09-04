"""Tests for picking which YouTube result to align lyrics against (offline).

Fixtures are real `ytsearch5:` output captured while designing this, so the
cases are the ones the selector actually faces.
"""
from karaoke import source_select as ss


def _c(title, duration=None, uploader=""):
    return {"url": f"https://youtu.be/{abs(hash(title)) % 10**6}",
            "title": title, "duration": duration, "uploader": uploader}


# Real ytsearch5 output for "Kyuss - Apothecaries Weight". LRCLIB says 319s.
KYUSS = [
    _c("Apothecaries' Weight", 322, "Kyuss - Topic"),
    _c("Kyuss - 15 - Apothecaries' Weight (Live Essen 1995)", 326, "Kyussist"),
    _c("Kyuss - Apothecaries' Weight (Video)", 326, "Finn Länd"),
    _c("Apothecaries' Weight - KYUSS", 322, "Tecla2222"),
    _c("Kyuss - Apothecaries' Weight (Guitar Cover)", 324, "Peju"),
]

# Real ytsearch5 output for "Peggy Lee - Fever". Note the 71-minute album.
PEGGY = [
    _c("Peggy Lee - Fever (Official Video)", 256, "Peggy Lee"),
    _c("Fever", 204, "Peggy Lee"),
    _c("Peggy Lee ~ Fever (1967 TV Special Performance)", 177, "Peggy Lee"),
    _c("Peggy Lee - Fever (Full Album)", 4273, "Lounge Sensation TV"),
    _c("Peggy Lee - \"Fever\" 1961 [Reelin' In The Years]", 213, "ReelinInTheYears66"),
]


# --- duration guard --------------------------------------------------------

def test_duration_ok_within_tolerance():
    assert ss.duration_ok(322, 319)          # different master, same track
    assert ss.duration_ok(319, 319)


def test_duration_ok_rejects_full_album():
    """The 71-minute upload is the failure this guard exists to stop."""
    assert not ss.duration_ok(4273, 256)


def test_duration_ok_rejects_short_clip():
    assert not ss.duration_ok(45, 256)


def test_duration_ok_permits_unknowns():
    """Reject on evidence only — never on its absence."""
    assert ss.duration_ok(None, 256)
    assert ss.duration_ok(256, None)
    assert ss.duration_ok(None, None)


# --- scoring ---------------------------------------------------------------

def test_topic_uploader_outranks_cover():
    topic = ss.score_candidate(KYUSS[0], "Kyuss", "Apothecaries' Weight")
    cover = ss.score_candidate(KYUSS[4], "Kyuss", "Apothecaries' Weight")
    assert topic > cover


def test_live_recording_is_penalised():
    studio = ss.score_candidate(KYUSS[0], "Kyuss", "Apothecaries' Weight")
    live = ss.score_candidate(KYUSS[1], "Kyuss", "Apothecaries' Weight")
    assert studio > live


def test_artist_owned_channel_beats_third_party():
    own = ss.score_candidate(PEGGY[0], "Peggy Lee", "Fever")
    third = ss.score_candidate(PEGGY[4], "Peggy Lee", "Fever")
    assert own > third


# --- selection ------------------------------------------------------------

def test_selects_topic_audio_over_higher_ranked_noise():
    best = ss.select_best_source(KYUSS, "Kyuss", "Apothecaries' Weight", 319)
    assert best["uploader"] == "Kyuss - Topic"
    assert best["duration"] == 322


def test_selects_official_video_and_never_the_album():
    best = ss.select_best_source(PEGGY, "Peggy Lee", "Fever", 256)
    assert best["title"] == "Peggy Lee - Fever (Official Video)"


def test_full_album_rejected_even_when_it_scores():
    """Duration is a hard gate, not a tiebreaker."""
    best = ss.select_best_source([PEGGY[3]], "Peggy Lee", "Fever", 256)
    assert best is None


def test_returns_none_when_everything_is_wrong_length():
    wrong = [_c("Fever (Live)", 900, "x"), _c("Fever (Clip)", 20, "y")]
    assert ss.select_best_source(wrong, "Peggy Lee", "Fever", 256) is None


def test_falls_back_to_scoring_when_no_reference_duration():
    """With no reference, behaviour must not get worse than before."""
    best = ss.select_best_source(KYUSS, "Kyuss", "Apothecaries' Weight", None)
    assert best["uploader"] == "Kyuss - Topic"


def test_ties_keep_youtube_relevance_order():
    a, b = _c("Song", 200, "chan"), _c("Song", 200, "chan")
    assert ss.select_best_source([a, b], "Artist", "Song", 200) is a


def test_skips_candidates_without_url():
    assert ss.select_best_source([{"title": "x", "duration": 200}],
                                 "A", "Song", 200) is None


def test_empty_candidate_list():
    assert ss.select_best_source([], "A", "B", None) is None
