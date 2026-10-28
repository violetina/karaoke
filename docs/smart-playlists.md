# Smart playlists

`make smart-playlists` rebuilds a fixed set of playlists from what the library
knows about each song. They are ordinary saved playlists (TUI `L`, graph
"Load Playlist") and, with `PUSH=1`, YouTube Music playlists the kiosk can play.

```bash
make smart-playlists DRY=1      # preview: counts and the first songs, saves nothing
make smart-playlists            # rebuild the saved playlists
make smart-playlists PUSH=1     # ... and sync the YouTube Music playlists
python -m karaoke.smart_playlists --list
python -m karaoke.smart_playlists --only "Smart: Dreamy" --push
```

## The lists

**Genre: …** — Blues, Jazz, Folk, Soul, Reggae, Country, Latin, Ambient,
Electronic, Hip Hop, Punk, Metal. An artist's genres come from
`artist_genres`: MusicBrainz genre votes for artists resolved by
`scripts/resolve_artists.py` (weight = votes), otherwise one row per tag from
the seed list or the name-searched classifier. Tracks of unclassified artists
fall back to the CLAP genre guess on the song.

An artist's **main** genre is the broad genre with the most weight behind it
(Frank Zappa: rock by far, jazz a distant second). An artist counts for a list
only when the genre has more than half the weight of its main one, so one
"blues rock" tag no longer puts The Doors in Blues. These lists are narrower
than the TUI's `H` filter, which takes any tag. Ranked:

1. artists whose main genre this is,
2. other artists strong enough in it,
3. tracks known only by the CLAP guess (least reliable; why there is no
   Shoegaze list — it put Billie Holiday there).

Within each, the more dedicated artist (fewer broad genres) first.

**Smart: …** — rules over the mood timeline (`track_moods`, current
`MOOD_VERSION`) and CLAP:

| List | Rule |
|---|---|
| Bittersweet & Ironic | the timeline's `contrast` is `bittersweet` (warm sound, dark words) or `ironic` (cynical tone) |
| Slow Burn | first section below zero arousal, last section 0.5+ higher |
| Dreamy | dominant feeling `dreamy` or `wistful` |
| Blue & Frustrated | `frustrated` or `blue` — the blues, not anger |
| Longing | `longing` or `melancholic` |
| Romantic | `romantic`, `tender`, `hopeful` |
| Party | `festive`, `cheerful`, `energetic`, `uplifting` |
| Mood Swings | three or more swings, most per minute first |
| More Sunday / More Komijn | nearest songs to the CLAP centroid of that saved playlist, minus its own songs |
| Radio Discoveries Set | the songs radio mode found, sequenced |
| Sing-along Favourites | most played songs with synced lyrics |

## How every list is shaped

1. **Playable only** — a YouTube / YouTube Music source (the kiosk plays them).
2. **Karaoke first** — synced lyrics, then plain lyrics, then none; most played next.
3. **Two songs per act, one version per song** — "Call Me (12'' Version)" and a
   cover by another act count as the same song (`song_key`). An act is the
   first credited MusicBrainz artist when the track is resolved, so spellings
   ("Bonnie "Prince" Billy", "Bonnie Prince Billy") and guest credits
   ("Gorillaz feat. …") are one act; otherwise the artist name.
4. **Genre family per song** — the artist's strong broad genres (or the CLAP
   guess's), and of those the one the list leans to most. A genre-less song
   borrows the family of close-sounding listmates. A family with at most two
   songs among the list's 198 candidates is the odd one out and only fills a
   list that would otherwise come up short.
5. **99 songs** — the TUI's own queue and collector still cap at 50.
6. **Flow order** — starting from the rule's best pick, each next song is the
   one that follows the current one best: CLAP timbre, plus mood hand-off
   (0.5), Camelot key (0.1), tempo (0.1) and chord motion (0.05), minus a
   penalty when the genre families differ (0.15 related, 0.4 unrelated), and
   never the same act back to back when anything else fits. A family with
   more than five songs plays as one block. The list plays like a set, not a
   sorted table.

## Refreshing

A rebuild replaces each list's *contents* in place:

- the saved playlist `smart-<name>` (`source_kind = 'smart'`) is rewritten and
  keeps its YouTube Music link;
- with `PUSH=1` the YouTube Music playlist of the same title is reused: songs
  that no longer belong are removed, new ones added. Its id — and any kiosk
  link to it — survives.

Nothing is ever deleted. Re-run after anything that changes the inputs: a mood
backfill (the mood lists fill as `track_moods` does), new radio discoveries,
or edits to the `sunday` / `Komijn 2025` playlists.

Adding a list is one `Rule` in `RULES` (`src/karaoke/smart_playlists.py`): a
name starting `Genre: ` or `Smart: `, a description, and a selector returning
track ids best first. Shaping and syncing are shared.
