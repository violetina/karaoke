"""Virtual MPRIS D-Bus Service for Karaoke Platform (org.mpris.MediaPlayer2.karaoke).

Exposes an MPRIS 2.2 compliant D-Bus interface so Linux desktop media keys,
system tray applets, and media controllers can monitor and control playback
on Chrome CDP (YouTube Music), Spotify, or local audio players.
"""
from __future__ import annotations

import asyncio
import argparse
import sys
from typing import Any, Optional, Dict

from .logger import log
from .player_open import open_song_url, try_chrome_cdp_navigate

try:
    from dbus_next.service import ServiceInterface, method, dbus_property, PropertyAccess
    from dbus_next.aio import MessageBus
    from dbus_next import Variant
    DBUS_NEXT_AVAILABLE = True
except ImportError:
    DBUS_NEXT_AVAILABLE = False
    class ServiceInterface:
        def __init__(self, *args, **kwargs):
            pass
        def emit_properties_changed(self, changed_properties, invalidated_properties=None):
            pass
    def method(*args, **kwargs): return lambda f: f
    class _MockProp:
        def __init__(self, fn, setter_fn=None):
            self.fn = fn
            self.setter_fn = setter_fn
        def __get__(self, instance, owner=None):
            if instance is None:
                return self
            return self.fn(instance)
        def __set__(self, instance, value):
            if self.setter_fn:
                self.setter_fn(instance, value)
        def setter(self, sfn):
            return _MockProp(self.fn, sfn)
    def dbus_property(*args, **kwargs): return lambda f: _MockProp(f)
    class PropertyAccess: READ = "read"; WRITE = "write"; READWRITE = "readwrite"
    class Variant:
        def __init__(self, signature, value): self.signature = signature; self.value = value


class MediaPlayer2RootInterface(ServiceInterface):
    """org.mpris.MediaPlayer2 interface implementation."""

    def __init__(self, service_manager: KaraokeMprisService):
        super().__init__("org.mpris.MediaPlayer2")
        self.mgr = service_manager

    @method()
    def Raise(self):
        log.info("MPRIS Raise called")

    @method()
    def Quit(self):
        log.info("MPRIS Quit called")
        self.mgr.stop()

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return "Karaoke Platform"

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return ["http", "https", "file", "spotify"]

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return ["audio/mpeg", "audio/ogg", "audio/flac", "audio/webm"]


class MediaPlayer2PlayerInterface(ServiceInterface):
    """org.mpris.MediaPlayer2.Player interface implementation."""

    def __init__(self, service_manager: KaraokeMprisService):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self.mgr = service_manager
        self._playback_status = "Stopped"
        self._loop_status = "None"
        self._volume = 1.0
        self._position = 0  # microseconds
        self._metadata: Dict[str, Variant] = {
            "mpris:trackid": Variant("o", "/org/mpris/MediaPlayer2/Track/0"),
            "xesam:artist": Variant("as", ["Karaoke"]),
            "xesam:title": Variant("s", "Idle"),
        }

    @method()
    def Next(self):
        log.info("MPRIS Next command received")
        self.mgr.dispatch_control("next")

    @method()
    def Previous(self):
        log.info("MPRIS Previous command received")
        self.mgr.dispatch_control("previous")

    @method()
    def Pause(self):
        log.info("MPRIS Pause command received")
        self._playback_status = "Paused"
        self.emit_properties_changed({"PlaybackStatus": "Paused"})
        self.mgr.dispatch_control("pause")

    @method()
    def PlayPause(self):
        log.info("MPRIS PlayPause command received")
        new_status = "Paused" if self._playback_status == "Playing" else "Playing"
        self._playback_status = new_status
        self.emit_properties_changed({"PlaybackStatus": new_status})
        self.mgr.dispatch_control("play_pause")

    @method()
    def Stop(self):
        log.info("MPRIS Stop command received")
        self._playback_status = "Stopped"
        self.emit_properties_changed({"PlaybackStatus": "Stopped"})
        self.mgr.dispatch_control("stop")

    @method()
    def Play(self):
        log.info("MPRIS Play command received")
        self._playback_status = "Playing"
        self.emit_properties_changed({"PlaybackStatus": "Playing"})
        self.mgr.dispatch_control("play")

    @method()
    def Seek(self, Offset: "x"):
        log.info("MPRIS Seek called with offset %s", Offset)

    @method()
    def SetPosition(self, TrackId: "o", Position: "x"):
        log.info("MPRIS SetPosition called for track %s pos %s", TrackId, Position)
        self._position = Position

    @method()
    def OpenUri(self, Uri: "s"):
        log.info("MPRIS OpenUri called: %s", Uri)
        open_song_url(Uri)

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self._playback_status

    @dbus_property(access=PropertyAccess.READWRITE)
    def LoopStatus(self) -> "s":
        return self._loop_status

    @LoopStatus.setter
    def LoopStatus(self, val: "s"):
        self._loop_status = val

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self._metadata

    @dbus_property(access=PropertyAccess.READWRITE)
    def Volume(self) -> "d":
        return self._volume

    @Volume.setter
    def Volume(self, val: "d"):
        self._volume = val

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return self._position

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True

    def update_metadata(self, artist: str, title: str, album: str = "", length_sec: Optional[float] = None):
        meta: Dict[str, Variant] = {
            "mpris:trackid": Variant("o", "/org/mpris/MediaPlayer2/Track/1"),
            "xesam:artist": Variant("as", [artist] if artist else ["Unknown Artist"]),
            "xesam:title": Variant("s", title or "Unknown Track"),
        }
        if album:
            meta["xesam:album"] = Variant("s", album)
        if length_sec:
            meta["mpris:length"] = Variant("x", int(length_sec * 1_000_000))
        self._metadata = meta
        self.emit_properties_changed({"Metadata": meta})


class KaraokeMprisService:
    """Manager for the org.mpris.MediaPlayer2.karaoke D-Bus service daemon."""

    BUS_NAME = "org.mpris.MediaPlayer2.karaoke"
    OBJECT_PATH = "/org/mpris/MediaPlayer2"

    def __init__(self):
        self.bus: Optional[MessageBus] = None
        self.root_iface: Optional[MediaPlayer2RootInterface] = None
        self.player_iface: Optional[MediaPlayer2PlayerInterface] = None
        self._running = False

    def dispatch_control(self, action: str):
        """Dispatch control action to Chrome CDP / RabbitMQ / Active target."""
        log.info("Dispatching player control action: %s", action)
        # 1. Try Chrome CDP Media Session simulation
        if action in ("play_pause", "play", "pause"):
            try_chrome_cdp_navigate("javascript:(function(){let v=document.querySelector('video,audio');if(v){v.paused?v.play():v.pause();}})()")

        # 2. Emit RabbitMQ event if pika/broker is available
        self._publish_event("control.player", {"action": action})

    def _publish_event(self, routing_key: str, payload: dict):
        try:
            import pika
            import json
            connection = pika.BlockingConnection(pika.ConnectionParameters(host="localhost", port=5672))
            channel = connection.channel()
            channel.exchange_declare(exchange="celeryev", exchange_type="topic", durable=True)
            channel.basic_publish(
                exchange="celeryev",
                routing_key=f"karaoke.{routing_key}",
                body=json.dumps(payload),
            )
            connection.close()
        except Exception:
            log.debug("RabbitMQ event publish skipped", exc_info=True)

    async def start_async(self):
        """Start the D-Bus service loop asynchronously."""
        if not DBUS_NEXT_AVAILABLE:
            log.error("dbus-next package is not installed; cannot start MPRIS D-Bus daemon")
            return

        self.bus = await MessageBus().connect()
        self.root_iface = MediaPlayer2RootInterface(self)
        self.player_iface = MediaPlayer2PlayerInterface(self)

        self.bus.export(self.OBJECT_PATH, self.root_iface)
        self.bus.export(self.OBJECT_PATH, self.player_iface)

        await self.bus.request_name(self.BUS_NAME)
        log.info("Registered D-Bus service %s at %s", self.BUS_NAME, self.OBJECT_PATH)
        self._running = True

        while self._running:
            await asyncio.sleep(1)

    def stop(self):
        self._running = False
        if self.bus:
            self.bus.disconnect()


def mpris_main(argv: list[str] | None = None):
    """CLI entrypoint for running the MPRIS D-Bus service daemon."""
    parser = argparse.ArgumentParser(description="Karaoke MPRIS D-Bus Controller Service")
    parser.add_argument("--daemon", action="store_true", help="Run in foreground daemon mode")
    args = parser.parse_args(argv)

    if not DBUS_NEXT_AVAILABLE:
        print("Error: dbus-next is required to run the MPRIS service daemon.")
        sys.exit(1)

    service = KaraokeMprisService()
    print(f"Starting Karaoke MPRIS D-Bus daemon ({KaraokeMprisService.BUS_NAME})...")
    try:
        asyncio.run(service.start_async())
    except KeyboardInterrupt:
        print("\nStopping MPRIS daemon.")
        service.stop()


if __name__ == "__main__":
    mpris_main()
