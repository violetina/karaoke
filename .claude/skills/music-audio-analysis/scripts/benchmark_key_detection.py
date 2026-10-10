#!/usr/bin/env python3
"""Benchmark musical key detection against documented ground truth.

Run this BEFORE and AFTER any change to a key-detection algorithm. A key
detector that has not been scored against known-key tracks should not ship.

Usage:
    python benchmark_key_detection.py tracks.json [--profiles edma,bgate,...]

tracks.json is a list of [audio_path, label, ground_truth_key]:

    [
      ["/path/Blinding Lights.mp3", "The Weeknd - Blinding Lights", "F minor"],
      ["/path/Billie Jean.mp3",     "MJ - Billie Jean",             "F# minor"]
    ]

Requires: pip install essentia  (Python 3.14: essentia==2.1b6.dev1438)
See references/key-detection-benchmark.md for a vetted starting track list.
"""

import argparse
import json
import random
import re
import sys
from collections import defaultdict

try:
    import essentia
    import essentia.standard as es

    essentia.log.infoActive = False
    essentia.log.warningActive = False
except ImportError:
    sys.exit("essentia not installed. Try: pip install essentia")

SR = 44100
DEFAULT_PROFILES = ["edma", "bgate", "temperley", "krumhansl", "shaath"]
ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


def analysis_windows(duration, seed=42):
    """Intro / mid / late / seeded-random windows.

    Seeded so runs are reproducible and comparable across algorithm versions.
    Short clips (e.g. 30s previews) get halves instead.
    """
    if duration <= 70:
        windows = [("full", 0.0, duration)]
        if duration > 20:
            half = duration / 2
            windows += [("first-half", 0.0, half), ("second-half", half, half)]
        return windows

    windows = [
        ("intro", 0.0, 30.0),
        ("mid", duration * 0.35, 30.0),
        ("late", duration * 0.70, 30.0),
    ]
    rnd = random.Random(seed)
    for i in range(3):
        start = rnd.uniform(0.10 * duration, 0.85 * duration - 25)
        windows.append((f"rand{i + 1}@{int(start)}s", start, 25.0))
    return windows


def normalize_key(text):
    """'F# minor (musicnotes)' -> 'F# MINOR' for tolerant comparison."""
    if not text:
        return ""
    cleaned = re.sub(r"\(.*?\)", " ", text).strip()
    match = re.match(r"([A-Ga-g][#b]?)\s*(maj|min)", cleaned, re.IGNORECASE)
    if not match:
        return cleaned.upper()
    note, mode = match.group(1), match.group(2)
    note = note[0].upper() + note[1:].replace("B", "b")
    note = ENHARMONIC.get(note, note)
    return f"{note} {'MINOR' if mode.lower() == 'min' else 'MAJOR'}"


def detect(audio, profile):
    key, scale, strength = es.KeyExtractor(profileType=profile, sampleRate=SR)(audio)
    return f"{key} {scale.title()}", float(strength)


def analyze(path, profile, verbose=False):
    """Strength-weighted vote across windows; full track weighted 1.5x.

    The full track is the single most informative view, but on real tracks it
    is sometimes wrong where the window majority is right — so it gets extra
    weight without an outright veto.
    """
    audio = es.MonoLoader(filename=path, sampleRate=SR)()
    duration = len(audio) / SR

    weights = defaultdict(float)
    per_window = []
    for name, start, length in analysis_windows(duration):
        segment = audio[int(start * SR):int((start + length) * SR)]
        if len(segment) < SR * 5:
            continue
        key, strength = detect(segment, profile)
        weights[key] += max(strength, 0.0)
        per_window.append((name, key, strength))
        if verbose:
            print(f"     {name:<16}{key:<12}strength={strength:.2f}")

    full_key, full_strength = detect(audio, profile)
    weights[full_key] += max(full_strength, 0.0) * 1.5

    total = sum(weights.values()) or 1.0
    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)
    best, best_weight = ranked[0]
    agree = sum(1 for _, k, _ in per_window if k == best)

    if verbose:
        print(f"     {'-' * 52}\n     full-track: {full_key} ({full_strength:.2f})")

    return {
        "vote": best,
        "confidence": round(best_weight / total, 3),
        "agreement": f"{agree}/{len(per_window)}",
        "full_track": full_key,
        "runner_up": ranked[1][0] if len(ranked) > 1 else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", help="JSON list of [path, label, truth]")
    ap.add_argument("--profiles", default=",".join(DEFAULT_PROFILES))
    ap.add_argument("--verbose", action="store_true",
                    help="print per-window detail for the first profile")
    args = ap.parse_args()

    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    tracks = json.load(open(args.fixture))
    rows, score = [], defaultdict(int)

    for path, label, truth in tracks:
        print(f"\n{'=' * 78}\n{label}   truth: {truth}\n{'=' * 78}")
        row = {"track": label, "truth": truth}
        for profile in profiles:
            try:
                res = analyze(path, profile,
                              verbose=args.verbose and profile == profiles[0])
            except Exception as exc:  # keep going; one bad file shouldn't abort
                print(f"  [{profile:<10}] FAILED: {exc}")
                row[profile] = "ERROR"
                continue
            ok = normalize_key(res["vote"]) == normalize_key(truth)
            score[profile] += ok
            row[profile] = res["vote"]
            print(f"  [{profile:<10}] {'OK ' if ok else 'XX '}"
                  f"vote={res['vote']:<12} full={res['full_track']:<12} "
                  f"conf={res['confidence']:.2f} agree={res['agreement']}")
            if res["confidence"] < 0.5:
                print(f"  {'':<13}low confidence — ambiguous; "
                      f"runner-up {res['runner_up']}")
        rows.append(row)

    n = len(tracks)
    print(f"\n\n{'=' * 78}\nSCOREBOARD ({n} tracks)\n{'=' * 78}")
    for profile in profiles:
        print(f"  {profile:<12}{score[profile]}/{n}")
    best = max(profiles, key=lambda p: score[p]) if profiles else None
    if best:
        print(f"\n  best: {best} ({score[best]}/{n})")
    print("\n  NOTE: a handful of pop tracks is a smoke test, not a benchmark.")
    print("  Report confidence/agreement to the user; never claim unmeasured accuracy.")

    json.dump(rows, open("key_benchmark_results.json", "w"), indent=2)
    print("  wrote key_benchmark_results.json")


if __name__ == "__main__":
    main()
