---
name: mpris-integration
description: Control Linux media players via MPRIS and playerctl.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [mpris, playerctl, linux, desktop, media, player, vlc, spotify]
---

# MPRIS Integration with playerctl

This skill covers interaction with Linux desktop media players that support the
MPRIS D-Bus interface. This includes most modern media players like VLC, Spotify,
and web browsers playing media. The primary tool is `playerctl`.

## When to use

- User wants to get the currently playing song from their desktop.
- User wants to control a desktop media player (play, pause, next, etc.).
- User wants to continuously monitor for track changes.

## Key commands and patterns

### One-shot metadata

The simplest use case is to get the current track's metadata.

```bash
playerctl metadata --format "{{artist}} - {{title}}"
```

### Continuous monitoring

To follow track changes, use the `--follow` option with the `metadata` command.

```bash
playerctl --follow metadata -f "{{artist}} - {{title}}"
```

### Player Control

`playerctl` can also send commands to the player.

```bash
playerctl play-pause
playerctl next
playerctl previous
playerctl stop
```

## Pitfalls

- **Multiple Players:** If multiple MPRIS-enabled players are running, `playerctl`
  may target the wrong one. Use `playerctl -l` to list all players and then
  target a specific one with the `-p` or `--player` flag.
- **Metadata Normalization:** Player metadata can be inconsistent. For example,
  VLC may include the artist's name in both the artist and title fields. It's
  important to normalize this data before using it.
- **`--follow` is an option, not a command:** The correct incantation is
  `playerctl --follow metadata ...`, not `playerctl follow ...`.
