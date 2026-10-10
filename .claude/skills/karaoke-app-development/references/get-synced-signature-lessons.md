# get_synced() Signature and Player Loop Handler Lessons

## Core Function Signature

`get_synced()` in `src/karaoke/player.py` expects a `SongRef` object as its first positional parameter:

```python
def get_synced(
    ref: SongRef,
    *,
    use_cache: bool = True,
    transcribe: bool = False,
    force_transcribe: bool = False,
    lyrics_file: Optional[str] = None,
    stats_mode: Optional[str] = None,
) -> Lyrics:
```

## Common Pitfall

Calling `get_synced(pb.artist, pb.title, ...)` or `get_synced(ref.artist, ref.title, ...)` with individual strings will raise a `TypeError` at runtime.

## Correct Pattern for Player Loops (Spotify, Radio, MPRIS)

Always construct a `SongRef` instance before calling `get_synced()`:

```python
ref = SongRef(
    artist=pb.artist,
    title=pb.title,
    duration=pb.duration_ms / 1000.0 if getattr(pb, "duration_ms", None) else None,
    source="spotify",
)
ly = get_synced(ref, use_cache=use_cache, stats_mode="spotify")
```
