import sys
import re

with open("src/karaoke/stage_view.py", "r") as f:
    content = f.read()

# Add chord_cpm and fifth_ratio to local vars
content = re.sub(r'    key: Optional\[str\] = None\n    energy: Optional\[float\] = None',
                 r'    key: Optional[str] = None\n    energy: Optional[float] = None\n    chord_cpm: Optional[float] = None\n    fifth_ratio: Optional[float] = None',
                 content)

# Add opensearch lookup
lookup_code = """
        except Exception as exc:
            log.debug("stage_view: db lookup failed: %s", exc)

    if track_id is not None:
        try:
            from . import osclient
            from .key_progression import PROGRESSION_INDEX, doc_id
            from opensearchpy.exceptions import NotFoundError
            client = osclient.client()
            if client:
                try:
                    resp = client.get(index=PROGRESSION_INDEX, id=doc_id(track_id))
                    if resp and resp.get("found"):
                        src = resp["_source"]
                        chord_cpm = src.get("chord_changes_per_minute")
                        fifth_ratio = src.get("fifth_ratio")
                except NotFoundError:
                    pass
        except Exception as exc:
            log.debug("stage_view: opensearch chord lookup failed: %s", exc)

    # 3. Look up active upcoming queue items"""

content = content.replace('        except Exception as exc:\n            log.debug("stage_view: db lookup failed: %s", exc)\n\n    # 3. Look up active upcoming queue items', lookup_code)

# Add to returned dict
content = re.sub(r'        "bpm": bpm,\n        "key": key,\n        "energy": energy,',
                 r'        "bpm": bpm,\n        "key": key,\n        "energy": energy,\n        "chord_cpm": chord_cpm,\n        "fifth_ratio": fifth_ratio,',
                 content)

with open("src/karaoke/stage_view.py", "w") as f:
    f.write(content)
