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
Electronic, Hip Hop, Punk, Metal. The match is the TUI's `H` genre filter: an
artist's genres (broad or specific) when the artist is classified, the CLAP
genre guess on the track otherwise. Ranking is by how much the act *belongs* to
the genre, because every artist genre has weight 1.0 and multi-genre acts
(Blondie: rock, pop, punk, soul via disco, electronic) would otherwise open
lists like Soul:

1. dedicated acts — at most two broad genres,
2. broader acts,
3. tracks known only by the CLAP guess (least reliable; why there is no
   Shoegaze list — it put Billie Holiday there).

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
   cover by another act count as the same song (`song_key`).
4. **50 songs** — the YouTube Music API slows and fails on longer playlists.
5. **Flow order** — starting from the rule's best pick, each next song is the
   one the DJ booth's `flow` scoring (CLAP timbre, mood hand-off, key, tempo)
   likes most after the current one, avoiding the same act back to back. The
   list plays like a set, not a sorted table.

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
