# Song identification on Linux (songrec / Shazam)

Condensed knowledge bank from a working setup session. Goal: identify the song
currently playing through the speakers, from the CLI, without a paid API key.

## Acquisition (Gentoo)

`media-sound/songrec` is **already packaged in GURU** — do not author an ebuild
and do not `cargo install`. As of this writing: v0.7.3, masked only by the
`~amd64` keyword.

```bash
emerge -s songrec                      # reveals "[ Masked ]"
emerge -pv media-sound/songrec         # "masked by: ~amd64 keyword"
printf 'media-sound/songrec ~amd64\n' | sudo tee -a \
  /etc/portage/package.accept_keywords/sound
sudo emerge -v media-sound/songrec
```

The ebuild's `DEPEND` lists gtk, libadwaita, ffmpeg, pipewire, openssl,
libpulse, libsoup, glib, graphene, cairo, gdk-pixbuf, pango — alarming, but on
a desktop system it resolved to **1 new package, 92 KiB**. Always check with
`emerge -pv ... | grep -c '^\[ebuild'` before reporting cost to the user.

Other distros: songrec is a Rust project (github.com/marin-m/SongRec); check
the native package manager before falling back to `cargo install`.

## CLI surface (non-obvious — worth keeping)

```
songrec recognize [input_file] [-d DEV] [-i SECS] [-j] [-c] [-l]
        listen                          # continuous, exposes MPRIS
        audio-file-to-recognized-song <file>
        microphone-to-recognized-song [-d DEV] [-i SECS]
        audio-file-to-fingerprint <file>
        fingerprint-to-recognized-song <data-uri>
        gui | gui-norecording           # default action is the GUI
```

`recognize` is the one to script: it accepts either a file or live device input.

Key flags:
- `-d, --audio-device` — accepts a **monitor source name** directly.
- `-l, --list-devices` — enumerates inputs *and* monitors, then exits.
- `-i, --request-interval` — seconds between Shazam requests, **default 10**.
- `-j` / `-c` — full song info as JSON / CSV.

## Monitor capture: no ffmpeg hop needed

`songrec recognize --list-devices` lists monitor sources alongside mics:

```
alsa_input.pci-....HiFi__Mic1__source            (Digital Microphone)
alsa_output.pci-....HiFi__Speaker__sink.monitor  (Monitor of Speaker)
alsa_output.usb-....analog-stereo.monitor        (Monitor of USB Audio)
```

So the whole `parec`/`ffmpeg` -> temp-WAV -> `audio-file-to-recognized-song`
pipeline is unnecessary. Stream straight from the monitor:

```bash
songrec recognize -d "$(pactl get-default-sink).monitor" -j
```

Bonus: nothing is written to disk, so no stray capture of the user's desktop
audio is left behind in `/tmp`.

## Tuning: `-i` and the timeout interact

Default `-i` is 10s, so a 30s wall-clock timeout is roughly **3 Shazam
attempts**. For obscure or quiet tracks, raise the timeout rather than dropping
`-i` (lowering `-i` risks rate-limiting — upstream says increase it if
rate-limited).

## Exit-code semantics

- exit 0, non-empty stdout -> match found
- exit 0, **empty stdout** -> ran fine, no fingerprint match
- exit 124 (under `timeout`) -> never matched within the window

Device chatter goes to **stderr**; the actual match goes to stdout. Redirect
stderr away when scripting, and test stdout emptiness rather than exit code.

## Privacy — state this accurately

songrec queries **Shazam's servers**. It is NOT fully offline:
- what leaves the machine: the audio *fingerprint*
- what does not leave: the raw audio
- what touches disk: nothing, when streaming from a monitor

Do not describe this as "offline" or "no audio leaves the machine" without that
qualification — it overclaims and has to be corrected mid-conversation.

## Alternatives (need an API key)

- **AudD** — REST, trivial to script, free tier then paid.
- **ACRCloud** — REST, broader catalog incl. broadcast, paid tiers.

Both mean sending audio/fingerprints to a third party with an account attached;
songrec avoids the account and the key entirely.

## Do NOT auto-identify every voice recording

Wiring song recognition into the agent's general voice-record path would ship
ordinary spoken conversations to a third-party recognition service. Keep it an
explicit, user-invoked command.
