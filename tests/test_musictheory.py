"""Tests for pure music-theory helpers and key reconciliation."""

from karaoke.musictheory import (
    Key, compatible_keys, keys_equivalent, parallel_key, parse_key,
    reconcile_key, relative_key,
)


def test_parse_key_forms():
    assert parse_key("Am") == Key(9, "minor")
    assert parse_key("A minor") == Key(9, "minor")
    assert parse_key("C") == Key(0, "major")
    assert parse_key("C major") == Key(0, "major")
    assert parse_key("F#m") == Key(6, "minor")
    assert parse_key("Bb maj") == Key(10, "major")
    assert parse_key("g#min") == Key(8, "minor")


def test_parse_key_invalid():
    assert parse_key("") is None
    assert parse_key("H major") is None
    assert parse_key("banana") is None


def test_key_names_and_camelot():
    assert Key(9, "minor").name == "A minor"
    assert Key(9, "minor").short == "Am"
    assert Key(0, "major").short == "C"
    assert Key(9, "minor").camelot == "8A"
    assert Key(0, "major").camelot == "8B"


def test_relative_and_parallel():
    # C major and A minor are relatives (same key signature)
    assert relative_key(Key(0, "major")) == Key(9, "minor")
    assert relative_key(Key(9, "minor")) == Key(0, "major")
    assert parallel_key(Key(9, "minor")) == Key(9, "major")


def test_keys_equivalent_relatives():
    assert keys_equivalent(Key(9, "minor"), Key(0, "major"))  # Am == C
    assert keys_equivalent(Key(0, "major"), Key(0, "major"))
    assert not keys_equivalent(Key(9, "minor"), Key(2, "minor"))


def test_reconcile_relative_is_agreement():
    # detected Am, online says C major -> same tonality
    rec = reconcile_key(parse_key("Am"), parse_key("C major"))
    assert rec.agree is True
    assert rec.relation == "relative"
    assert rec.resolved == Key(0, "major")  # prefers the reference


def test_reconcile_exact():
    rec = reconcile_key(parse_key("C"), parse_key("C major"))
    assert rec.relation == "exact"
    assert rec.agree is True


def test_reconcile_parallel_not_agreement():
    rec = reconcile_key(parse_key("Am"), parse_key("A major"))
    assert rec.relation == "parallel"
    assert rec.agree is False


def test_reconcile_conflict():
    rec = reconcile_key(parse_key("Am"), parse_key("F# major"))
    assert rec.relation == "conflict"
    assert rec.agree is False


def test_reconcile_partial_and_unknown():
    assert reconcile_key(parse_key("Am"), None).relation == "partial"
    assert reconcile_key(None, parse_key("C")).relation == "partial"
    assert reconcile_key(None, None).relation == "unknown"


def test_compatible_keys_excludes_self():
    ck = compatible_keys(Key(0, "major"))
    assert Key(0, "major") not in ck
    assert Key(9, "minor") in ck  # relative
