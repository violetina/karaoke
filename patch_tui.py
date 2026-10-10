import sys
import re

with open("src/karaoke/tui.py", "r") as f:
    content = f.read()

opensearch_lookup = """        if not genre and self._current_track_id is not None:
            try:
                with localcache.connect() as conn:
                    genre = str(localcache.genre_for(self._current_track_id, conn) or "")
            except Exception:
                pass
                
        chord_cpm = None
        fifth_ratio = None
        if self._current_track_id is not None:
            try:
                from . import osclient
                from .key_progression import PROGRESSION_INDEX, doc_id
                client = osclient.client()
                if client:
                    resp = client.get(index=PROGRESSION_INDEX, id=doc_id(self._current_track_id), ignore=404)
                    if resp and resp.get("found"):
                        src = resp["_source"]
                        chord_cpm = src.get("chord_changes_per_minute")
                        fifth_ratio = src.get("fifth_ratio")
            except Exception:
                pass
"""
content = content.replace('        if not genre and self._current_track_id is not None:\n            try:\n                with localcache.connect() as conn:\n                    genre = str(localcache.genre_for(self._current_track_id, conn) or "")\n            except Exception:\n                pass\n', opensearch_lookup)


dance_floor_replace = """                dance = visuals.dance_floor_frame(
                    bpm,
                    elapsed,
                    width=max(20, pw),
                    height=max(6, ph),
                    num_dancers=self._dance_num_dancers,
                    mood=profile.dominant,
                    genre=genre,
                    energy=energy,
                    chord_cpm=chord_cpm,
                    fifth_ratio=fifth_ratio,
                )"""

content = re.sub(r'                dance = visuals\.dance_floor_frame\(\n                    bpm,\n                    elapsed,\n                    width=max\(20, pw\),\n                    height=max\(6, ph\),\n                    num_dancers=self\._dance_num_dancers,\n                    mood=profile\.dominant,\n                    genre=genre,\n                    energy=energy,\n                \)', dance_floor_replace, content)

with open("src/karaoke/tui.py", "w") as f:
    f.write(content)
