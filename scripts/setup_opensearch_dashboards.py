"""Generate and import OpenSearch Dashboards saved objects for Karaoke.

Creates index patterns, saved searches, visualizations, and four dashboards:
1. ``karaoke-artists-overview`` — MusicBrainz artists by genre, country, decade,
   type, record label, and top track count.
2. ``karaoke-lyrics-coverage`` — Synced vs unsynced lyrics coverage, breakdown
   by ``lyrics_source``, genre x synced status, mood, and top artists with or
   missing synced lyrics.
3. ``karaoke-lyric-lines`` — Full-text lyric line search over ``tracks-lines``
   with artist, title, timestamp, line text, and surrounding context.

4. ``karaoke-audio-analysis`` — Mood v2 and Chords v2 (sound-derived) breakdowns.

Index patterns omit ``timeFieldName`` on purpose so Discover and Dashboards do
not filter out documents outside the default "Last 15 minutes" window, and set
``sourceFilters`` to hide high-dimensional vector arrays in Discover.

Run:
    ``PYTHONPATH=src .venv/bin/python scripts/setup_opensearch_dashboards.py``
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
import urllib.request

DASHBOARDS_URL = "http://localhost:5601"
NDJSON_PATH = (
    Path(__file__).resolve().parent.parent
    / "deploy"
    / "opensearch-dashboards"
    / "saved-objects.ndjson"
)


def _index_pattern(
    obj_id: str, title: str, excluded_fields: list[str] | None = None
) -> dict[str, Any]:
    attrs: dict[str, Any] = {"title": title}
    if excluded_fields:
        attrs["sourceFilters"] = json.dumps(
            [{"value": f} for f in excluded_fields]
        )
    return {
        "type": "index-pattern",
        "id": obj_id,
        "attributes": attrs,
        "references": [],
    }


def _saved_search(
    obj_id: str,
    title: str,
    description: str,
    index_pattern_id: str,
    columns: list[str],
    sort: list[list[str]] | None = None,
    kuery: str = "",
) -> dict[str, Any]:
    search_source = {
        "query": {"query": kuery, "language": "kuery"},
        "filter": [],
        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
    }
    return {
        "type": "search",
        "id": obj_id,
        "attributes": {
            "title": title,
            "description": description,
            "columns": columns,
            "sort": sort or [],
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(search_source)
            },
        },
        "references": [
            {
                "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                "type": "index-pattern",
                "id": index_pattern_id,
            }
        ],
    }


def _vis(
    obj_id: str,
    title: str,
    vis_type: str,
    index_pattern_id: str,
    aggs: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
    kuery: str = "",
) -> dict[str, Any]:
    default_params: dict[str, Any] = {
        "metric": {
            "addTooltip": True,
            "addLegend": False,
            "type": "metric",
            "metric": {
                "percentageMode": False,
                "useRanges": False,
                "colorSchema": "Green to Red",
                "metricColorMode": "None",
                "colorsRange": [{"from": 0, "to": 100000}],
                "labels": {"show": True},
                "invertColors": False,
                "style": {
                    "bgFill": "#000",
                    "bgColor": False,
                    "labelColor": False,
                    "subText": "",
                    "fontSize": 48,
                },
            },
        },
        "pie": {
            "type": "pie",
            "addTooltip": True,
            "addLegend": True,
            "legendPosition": "right",
            "isDonut": True,
            "labels": {
                "show": True,
                "values": True,
                "last_level": True,
                "truncate": 100,
            },
        },
        "horizontal_bar": {
            "type": "histogram",
            "grid": {"categoryLines": False},
            "categoryAxes": [
                {
                    "id": "CategoryAxis-1",
                    "type": "category",
                    "position": "left",
                    "show": True,
                    "style": {},
                    "scale": {"type": "linear"},
                    "labels": {"show": True, "filter": True, "truncate": 120},
                    "title": {},
                }
            ],
            "valueAxes": [
                {
                    "id": "ValueAxis-1",
                    "name": "LeftAxis-1",
                    "type": "value",
                    "position": "bottom",
                    "show": True,
                    "style": {},
                    "scale": {"type": "linear", "mode": "normal"},
                    "labels": {
                        "show": True,
                        "rotate": 0,
                        "filter": True,
                        "truncate": 100,
                    },
                    "title": {"text": "Count"},
                }
            ],
            "seriesParams": [
                {
                    "show": True,
                    "type": "histogram",
                    "mode": "stacked",
                    "data": {"label": "Count", "id": "1"},
                    "valueAxis": "ValueAxis-1",
                    "drawLinesBetweenPoints": True,
                    "lineWidth": 2,
                    "showCircles": True,
                }
            ],
            "addTooltip": True,
            "addLegend": True,
            "legendPosition": "right",
            "times": [],
            "addTimeMarker": False,
        },
        "table": {
            "perPage": 15,
            "showPartialRows": False,
            "showMetricsAtAllLevels": False,
            "sort": {"columnIndex": None, "direction": None},
            "showTotal": False,
            "totalFunc": "sum",
            "percentageCol": "",
        },
    }[vis_type]
    if params:
        default_params.update(params)

    vis_state = {
        "title": title,
        "type": vis_type,
        "aggs": aggs,
        "params": default_params,
    }
    search_source = {
        "query": {"query": kuery, "language": "kuery"},
        "filter": [],
        "indexRefName": "kibanaSavedObjectMeta.searchSourceJSON.index",
    }
    return {
        "type": "visualization",
        "id": obj_id,
        "attributes": {
            "title": title,
            "visState": json.dumps(vis_state),
            "uiStateJSON": "{}",
            "description": "",
            "version": 1,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(search_source)
            },
        },
        "references": [
            {
                "name": "kibanaSavedObjectMeta.searchSourceJSON.index",
                "type": "index-pattern",
                "id": index_pattern_id,
            }
        ],
    }


def _terms_bucket(
    agg_id: str,
    field: str,
    label: str,
    size: int = 15,
    schema: str = "segment",
    order_agg: str = "1",
) -> dict[str, Any]:
    return {
        "id": agg_id,
        "enabled": True,
        "type": "terms",
        "schema": schema,
        "params": {
            "field": field,
            "size": size,
            "order": "desc",
            "orderBy": order_agg,
            "otherBucket": False,
            "missingBucket": False,
            "customLabel": label,
        },
    }


def _dashboard(
    obj_id: str,
    title: str,
    description: str,
    panels_spec: list[tuple[str, str, str, int, int, int, int]],
) -> dict[str, Any]:
    """Build a dashboard saved object.

    ``panels_spec`` items are ``(panel_type, target_id, title, x, y, w, h)``.
    """
    panels = []
    refs = []
    for idx, (ptype, tid, ptitle, x, y, w, h) in enumerate(panels_spec, start=1):
        panel_name = f"panel_{idx}"
        panels.append(
            {
                "version": "3.8.0",
                "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(idx)},
                "panelIndex": str(idx),
                "embeddableConfig": {"title": ptitle},
                "panelRefName": panel_name,
            }
        )
        refs.append({"name": panel_name, "type": ptype, "id": tid})

    return {
        "type": "dashboard",
        "id": obj_id,
        "attributes": {
            "title": title,
            "description": description,
            "hits": 0,
            "optionsJSON": json.dumps(
                {"useMargins": True, "syncColors": False, "hidePanelTitles": False}
            ),
            "panelsJSON": json.dumps(panels),
            "timeRestore": False,
            "version": 1,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"query": "", "language": "kuery"}, "filter": []}
                )
            },
        },
        "references": refs,
    }


def fetch_fields(url: str, pattern: str) -> list[dict[str, Any]]:
    """Field list for an index pattern, as Dashboards itself would cache it.

    Index patterns imported without a ``fields`` attribute are not refreshed
    automatically; every visualization then fails with "Could not locate that
    index-pattern-field". Embedding the list at import time avoids a manual
    "refresh field list" click per pattern.
    """
    q = "&".join(f"meta_fields={m}" for m in ("_source", "_id", "_type", "_index", "_score"))
    with urllib.request.urlopen(
        f"{url.rstrip('/')}/api/index_patterns/_fields_for_wildcard?pattern={pattern}&{q}",
        timeout=30,
    ) as resp:
        return json.loads(resp.read().decode("utf-8"))["fields"]


def attach_fields(url: str, objects: list[dict[str, Any]]) -> None:
    for obj in objects:
        if obj["type"] == "index-pattern":
            fields = fetch_fields(url, obj["attributes"]["title"])
            obj["attributes"]["fields"] = json.dumps(fields)


def build_saved_objects() -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []

    # 1. Index Patterns
    objs.append(_index_pattern("karaoke-artists", "karaoke-artists"))
    objs.append(
        _index_pattern(
            "tracks",
            "tracks",
            excluded_fields=["lyrics_vector", "sentiment_vector", "synced_lyrics"],
        )
    )
    objs.append(
        _index_pattern(
            "tracks-lines",
            "tracks-lines",
            excluded_fields=["line_vector"],
        )
    )

    # 2. Saved Searches
    objs.append(
        _saved_search(
            "karaoke-artists-search",
            "Artists Directory",
            "MusicBrainz artist profiles with genres, country, decade, and library track counts.",
            "karaoke-artists",
            [
                "name",
                "kind",
                "country",
                "began_decade",
                "main_genre",
                "broad_genres",
                "track_count",
                "record_labels",
            ],
            sort=[["track_count", "desc"]],
        )
    )
    objs.append(
        _saved_search(
            "karaoke-tracks-search",
            "Tracks & Lyrics Explorer",
            "All indexed karaoke tracks with genre, mood, and lyrics status.",
            "tracks",
            [
                "artist",
                "title",
                "album",
                "broad_genres",
                "audio_genre",
                "artist_genres",
                "dominant_mood",
                "has_synced",
                "lyrics_source",
                "duration",
            ],
            sort=[["track_id", "desc"]],
        )
    )
    objs.append(
        _saved_search(
            "karaoke-unsynced-tracks-search",
            "Tracks Missing Synced Lyrics",
            "Tracks without synced LRC lyrics (candidates for LRCLIB or Whisper alignment).",
            "tracks",
            [
                "artist",
                "title",
                "lyrics_source",
                "broad_genres",
                "audio_genre",
                "artist_genres",
                "dominant_mood",
                "source_kind",
                "source_url",
            ],
            sort=[["artist.raw", "asc"]],
            kuery="has_synced: false",
        )
    )
    objs.append(
        _saved_search(
            "karaoke-lyric-lines-search",
            "Lyric Lines Search",
            "Full-text search across every indexed lyric line with timestamp and context.",
            "tracks-lines",
            [
                "artist",
                "title",
                "line_index",
                "start_s",
                "text",
                "context",
                "source",
            ],
        )
    )

    # 3. Artists Visualizations
    count_metric = {
        "id": "1",
        "enabled": True,
        "type": "count",
        "schema": "metric",
        "params": {},
    }
    objs.append(
        _vis(
            "vis-artists-total",
            "Total MusicBrainz Artists",
            "metric",
            "karaoke-artists",
            [{**count_metric, "params": {"customLabel": "Resolved MB Artists"}}],
        )
    )
    objs.append(
        _vis(
            "vis-artists-kind",
            "Artists by Type (Person / Group)",
            "pie",
            "karaoke-artists",
            [count_metric, _terms_bucket("2", "kind", "Artist Type", size=8)],
        )
    )
    objs.append(
        _vis(
            "vis-artists-main-genre",
            "Artists by Main Genre",
            "pie",
            "karaoke-artists",
            [count_metric, _terms_bucket("2", "main_genre", "Main Genre", size=15)],
        )
    )
    objs.append(
        _vis(
            "vis-artists-broad-genres",
            "Artists by Broad Genre",
            "horizontal_bar",
            "karaoke-artists",
            [count_metric, _terms_bucket("2", "broad_genres", "Broad Genre", size=16)],
        )
    )
    objs.append(
        _vis(
            "vis-artists-country",
            "Artists by Country",
            "horizontal_bar",
            "karaoke-artists",
            [count_metric, _terms_bucket("2", "country", "Country", size=20)],
        )
    )
    objs.append(
        _vis(
            "vis-artists-decade",
            "Artists by Decade Started",
            "horizontal_bar",
            "karaoke-artists",
            [count_metric, _terms_bucket("2", "began_decade", "Decade", size=12)],
        )
    )
    objs.append(
        _vis(
            "vis-artists-top-tracks",
            "Top Artists by Library Track Count",
            "table",
            "karaoke-artists",
            [
                {
                    "id": "1",
                    "enabled": True,
                    "type": "max",
                    "schema": "metric",
                    "params": {"field": "track_count", "customLabel": "Tracks in Library"},
                },
                _terms_bucket("2", "name.raw", "Artist", size=25, schema="bucket", order_agg="1"),
                _terms_bucket("3", "main_genre", "Main Genre", size=1, schema="bucket", order_agg="1"),
                _terms_bucket("4", "country", "Country", size=1, schema="bucket", order_agg="1"),
            ],
        )
    )
    objs.append(
        _vis(
            "vis-artists-labels",
            "Top Record Labels",
            "table",
            "karaoke-artists",
            [
                {**count_metric, "params": {"customLabel": "Artists"}},
                _terms_bucket("2", "record_labels", "Record Label", size=20, schema="bucket"),
            ],
        )
    )

    # 4. Tracks & Lyrics Coverage Visualizations
    objs.append(
        _vis(
            "vis-tracks-total",
            "Total Indexed Tracks",
            "metric",
            "tracks",
            [{**count_metric, "params": {"customLabel": "Indexed Tracks"}}],
        )
    )
    objs.append(
        _vis(
            "vis-lyrics-synced-pie",
            "Synced vs Unsynced Lyrics",
            "pie",
            "tracks",
            [count_metric, _terms_bucket("2", "has_synced", "Has Synced Lyrics", size=5)],
        )
    )
    objs.append(
        _vis(
            "vis-lyrics-source-bar",
            "Tracks by Lyrics Source",
            "horizontal_bar",
            "tracks",
            [count_metric, _terms_bucket("2", "lyrics_source", "Lyrics Source", size=12)],
        )
    )
    objs.append(
        _vis(
            "vis-lyrics-genre-synced",
            "Lyrics Coverage by Broad Genre",
            "horizontal_bar",
            "tracks",
            [
                count_metric,
                _terms_bucket("2", "broad_genres.keyword", "Broad Genre", size=16, schema="segment"),
                _terms_bucket("3", "has_synced", "Has Synced Lyrics", size=2, schema="group"),
            ],
        )
    )
    objs.append(
        _vis(
            "vis-tracks-mood-pie",
            "Tracks by Dominant Mood",
            "pie",
            "tracks",
            [count_metric, _terms_bucket("2", "dominant_mood.keyword", "Dominant Mood", size=12)],
        )
    )
    objs.append(
        _vis(
            "vis-artists-missing-lyrics",
            "Top Artists Missing Synced Lyrics",
            "table",
            "tracks",
            [
                {**count_metric, "params": {"customLabel": "Unsynced Tracks"}},
                _terms_bucket("2", "artist.raw", "Artist", size=25, schema="bucket"),
            ],
            kuery="has_synced: false",
        )
    )
    objs.append(
        _vis(
            "vis-artists-with-lyrics",
            "Top Artists with Synced Lyrics",
            "table",
            "tracks",
            [
                {**count_metric, "params": {"customLabel": "Synced Tracks"}},
                _terms_bucket("2", "artist.raw", "Artist", size=25, schema="bucket"),
            ],
            kuery="has_synced: true",
        )
    )

    # 5. Lyric Lines Visualizations
    objs.append(
        _vis(
            "vis-lines-total",
            "Total Indexed Lyric Lines",
            "metric",
            "tracks-lines",
            [{**count_metric, "params": {"customLabel": "Indexed Lyric Lines"}}],
        )
    )
    objs.append(
        _vis(
            "vis-lines-source-pie",
            "Lyric Lines by Source",
            "pie",
            "tracks-lines",
            [count_metric, _terms_bucket("2", "source", "Source", size=10)],
        )
    )
    objs.append(
        _vis(
            "vis-lines-top-artists",
            "Top Artists by Indexed Lyric Lines",
            "horizontal_bar",
            "tracks-lines",
            [count_metric, _terms_bucket("2", "artist.raw", "Artist", size=20)],
        )
    )

    # 5b. Audio analysis (Mood v2 + Chords v2) visualizations
    def _avg(field: str, label: str) -> dict[str, Any]:
        return {"id": "1", "enabled": True, "type": "avg", "schema": "metric",
                "params": {"field": field, "customLabel": label}}

    objs.append(_vis("vis-mood2-coverage", "Tracks with Mood v2", "metric", "tracks",
                     [{**count_metric, "params": {"customLabel": "Tracks with Mood v2"}}],
                     kuery="audio_mood:*"))
    objs.append(_vis("vis-chords2-coverage", "Tracks with Chords v2", "metric", "tracks",
                     [{**count_metric, "params": {"customLabel": "Tracks with Chords v2"}}],
                     kuery="harmony_style:*"))
    objs.append(_vis("vis-audio-mood-pie", "Audio Mood (v2)", "pie", "tracks",
                     [count_metric, _terms_bucket("2", "audio_mood", "Mood", size=6)]))
    objs.append(_vis("vis-audio-feeling-bar", "Audio Feeling (v2)", "horizontal_bar", "tracks",
                     [count_metric, _terms_bucket("2", "audio_feeling", "Feeling", size=20)]))
    objs.append(_vis("vis-mood-by-genre", "Audio Mood by Broad Genre", "horizontal_bar", "tracks",
                     [count_metric,
                      _terms_bucket("2", "broad_genres.keyword", "Broad Genre", size=16),
                      _terms_bucket("3", "audio_mood", "Mood", size=6, schema="group")]))
    objs.append(_vis("vis-mood-arcs", "Most Common Mood Arcs", "table", "tracks",
                     [{**count_metric, "params": {"customLabel": "Tracks"}},
                      _terms_bucket("2", "mood_arc", "Mood Arc", size=15, schema="bucket")]))
    objs.append(_vis("vis-mood-swings-genre", "Mood Swings per Minute by Genre", "horizontal_bar", "tracks",
                     [_avg("mood_swings_per_minute", "Avg swings/min"),
                      _terms_bucket("2", "broad_genres.keyword", "Broad Genre", size=16)]))
    objs.append(_vis("vis-harmony-style-pie", "Harmony Style (Chords v2)", "pie", "tracks",
                     [count_metric, _terms_bucket("2", "harmony_style", "Harmony Style", size=10)]))
    objs.append(_vis("vis-key-mode-pie", "Major vs Minor Key", "pie", "tracks",
                     [count_metric, _terms_bucket("2", "key_mode", "Key Mode", size=3)]))
    objs.append(_vis("vis-harmony-by-genre", "Harmony Style by Broad Genre", "horizontal_bar", "tracks",
                     [count_metric,
                      _terms_bucket("2", "broad_genres.keyword", "Broad Genre", size=16),
                      _terms_bucket("3", "harmony_style", "Harmony Style", size=8, schema="group")]))
    objs.append(_vis("vis-blues-by-genre", "Blues Score by Broad Genre", "horizontal_bar", "tracks",
                     [_avg("blues_score", "Avg blues score"),
                      _terms_bucket("2", "broad_genres.keyword", "Broad Genre", size=16)]))
    objs.append(_vis("vis-jazz-artists", "Artists with Most ii-V-I Cadences", "table", "tracks",
                     [{"id": "1", "enabled": True, "type": "sum", "schema": "metric",
                       "params": {"field": "ii_v_i", "customLabel": "ii-V-I cadences"}},
                      _terms_bucket("2", "artist.raw", "Artist", size=20, schema="bucket")]))

    # 6. Three Dashboards
    objs.append(
        _dashboard(
            "karaoke-artists-overview",
            "Karaoke Artists Overview",
            "MusicBrainz artist metadata, genres, countries, decades, record labels, and library track counts.",
            [
                ("visualization", "vis-artists-total", "Total MusicBrainz Artists", 0, 0, 12, 10),
                ("visualization", "vis-artists-kind", "Artists by Type", 12, 0, 16, 10),
                ("visualization", "vis-artists-main-genre", "Artists by Main Genre", 28, 0, 20, 10),
                ("visualization", "vis-artists-broad-genres", "Artists by Broad Genre", 0, 10, 24, 14),
                ("visualization", "vis-artists-country", "Artists by Country", 24, 10, 24, 14),
                ("visualization", "vis-artists-decade", "Artists by Decade Started", 0, 24, 16, 14),
                ("visualization", "vis-artists-top-tracks", "Top Artists by Library Track Count", 16, 24, 18, 14),
                ("visualization", "vis-artists-labels", "Top Record Labels", 34, 24, 14, 14),
                ("search", "karaoke-artists-search", "Artists Directory", 0, 38, 48, 18),
            ],
        )
    )
    objs.append(
        _dashboard(
            "karaoke-lyrics-coverage",
            "Karaoke Lyrics & Library Coverage",
            "Synced vs unsynced lyrics coverage, lyrics sources, genre/mood breakdown, and per-artist gaps.",
            [
                ("visualization", "vis-tracks-total", "Total Indexed Tracks", 0, 0, 12, 10),
                ("visualization", "vis-lyrics-synced-pie", "Synced vs Unsynced Lyrics", 12, 0, 16, 10),
                ("visualization", "vis-tracks-mood-pie", "Tracks by Dominant Mood", 28, 0, 20, 10),
                ("visualization", "vis-lyrics-source-bar", "Tracks by Lyrics Source", 0, 10, 24, 14),
                ("visualization", "vis-lyrics-genre-synced", "Lyrics Coverage by Broad Genre", 24, 10, 24, 14),
                ("visualization", "vis-artists-missing-lyrics", "Top Artists Missing Synced Lyrics", 0, 24, 24, 15),
                ("visualization", "vis-artists-with-lyrics", "Top Artists with Synced Lyrics", 24, 24, 24, 15),
                ("search", "karaoke-unsynced-tracks-search", "Tracks Missing Synced Lyrics", 0, 39, 48, 18),
            ],
        )
    )
    objs.append(
        _dashboard(
            "karaoke-lyric-lines",
            "Karaoke Lyric Lines Search",
            "Search every lyric line across the library with artist, song title, timestamp, and context.",
            [
                ("visualization", "vis-lines-total", "Total Indexed Lyric Lines", 0, 0, 12, 10),
                ("visualization", "vis-lines-source-pie", "Lyric Lines by Source", 12, 0, 14, 10),
                ("visualization", "vis-lines-top-artists", "Top Artists by Indexed Lyric Lines", 26, 0, 22, 10),
                ("search", "karaoke-lyric-lines-search", "Lyric Lines Search", 0, 10, 48, 24),
            ],
        )
    )

    objs.append(
        _dashboard(
            "karaoke-audio-analysis",
            "Karaoke Audio Analysis (Mood v2 & Chords v2)",
            "Sound-derived mood and harmony: moods, feelings, arcs, swing rates, key mode, harmony style, blues and jazz markers.",
            [
                ("visualization", "vis-mood2-coverage", "Tracks with Mood v2", 0, 0, 12, 8),
                ("visualization", "vis-chords2-coverage", "Tracks with Chords v2", 12, 0, 12, 8),
                ("visualization", "vis-audio-mood-pie", "Audio Mood (v2)", 24, 0, 12, 8),
                ("visualization", "vis-key-mode-pie", "Major vs Minor Key", 36, 0, 12, 8),
                ("visualization", "vis-audio-feeling-bar", "Audio Feeling (v2)", 0, 8, 24, 14),
                ("visualization", "vis-harmony-style-pie", "Harmony Style (Chords v2)", 24, 8, 24, 14),
                ("visualization", "vis-mood-by-genre", "Audio Mood by Broad Genre", 0, 22, 24, 15),
                ("visualization", "vis-harmony-by-genre", "Harmony Style by Broad Genre", 24, 22, 24, 15),
                ("visualization", "vis-mood-swings-genre", "Mood Swings per Minute by Genre", 0, 37, 24, 14),
                ("visualization", "vis-blues-by-genre", "Blues Score by Broad Genre", 24, 37, 24, 14),
                ("visualization", "vis-mood-arcs", "Most Common Mood Arcs", 0, 51, 24, 15),
                ("visualization", "vis-jazz-artists", "Artists with Most ii-V-I Cadences", 24, 51, 24, 15),
            ],
        )
    )

    return objs


def write_ndjson(path: Path, objects: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(obj, separators=(",", ":")) for obj in objects]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def import_ndjson(url: str, path: Path) -> dict[str, Any]:
    boundary = "----KaraokeDashboardsBoundary7MA4YWxkTrZu0gW"
    content = path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: application/ndjson\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/saved_objects/_import?overwrite=true",
        data=body,
        headers={
            "osd-xsrf": "true",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def set_default_index_pattern(url: str, index_pattern_id: str = "tracks") -> None:
    data = json.dumps({"value": index_pattern_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{url.rstrip('/')}/api/opensearch-dashboards/settings/defaultIndex",
        data=data,
        headers={"osd-xsrf": "true", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--url", default=DASHBOARDS_URL, help="OpenSearch Dashboards base URL")
    ap.add_argument("--output", type=Path, default=NDJSON_PATH, help="NDJSON path to write")
    ap.add_argument("--generate-only", action="store_true", help="only write NDJSON file")
    args = ap.parse_args()

    objs = build_saved_objects()
    if not args.generate_only:
        attach_fields(args.url, objs)
    write_ndjson(args.output, objs)
    print(f"Wrote {len(objs)} saved objects to {args.output}")

    if args.generate_only:
        return 0

    res = import_ndjson(args.url, args.output)
    print(f"Import result: success={res.get('success')}, successCount={res.get('successCount')}, errors={res.get('errors', [])}")
    if not res.get("success"):
        return 1

    set_default_index_pattern(args.url, "tracks")
    print("Set defaultIndex = 'tracks'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
