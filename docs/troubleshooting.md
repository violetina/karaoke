# Trouble shooting

## Help
```make
make help
```
 
## Radio Keep Going leaves the queue empty

New radio songs get matches only after their audio is downloaded and
analysed (CLAP vector). If `postprocess_queue.needs_postprocessing(track_id)`
still lists `analysis`/`clap` after the Celery chain finished, the download
failed:

```bash
journalctl --user -u karaoke-celery-worker --since today | grep -c 'download failed'
journalctl --user -u karaoke-celery-worker --since today | grep 'page needs to be reloaded'
```

YouTube changes break older yt-dlp builds. Upgrade it, restart the workers and
re-queue the song:

```bash
.venv/bin/pip install -U yt-dlp
systemctl --user restart karaoke-celery-worker karaoke-celery-clap karaoke-celery-sync
PYTHONPATH=src .venv/bin/python -c "from karaoke.tasks import enqueue_postprocess; enqueue_postprocess({'artist': 'ARTIST', 'title': 'TITLE', 'url': '', 'full': True})"
```

See [Radio mode → Keep Going](modes/radio.md#keep-going-from-the-radio-collectionradio_keep_going_refill).

## Lyrics early/late on the music video

The video often has an intro, so its lyrics use a per-track offset found by comparing the
two audio streams. Re-check one track, even when an offset is already stored:

```bash
make batch-sync-av TRACK_ID=<id> FORCE_WAVEFORM=1
```

A big jump at low confidence is refused and the stored offset kept (`kept_stored` in
`av_sync_log`). See [Audio vs video lyrics sync](av-sync.md).

## Update documentation
```make
make docs-write
```
 
### Dependencies
 - make
 - python >=3.11 
	 +  `pip install . `		Installs project

## Update make schema



### makefile2graph 
- git: 	
	- url = https://github.com/lindenb/makefile2graph.git

	- Creates a graph of dependencies from GNU-Make
- TODO:
	- proper add git upstream as dependency


## README
--8<-- "README.md"
