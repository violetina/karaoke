"""Tests for the virtual MPRIS D-Bus Controller service."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from karaoke import mpris_service as ms


def test_mpris_player_interface_methods():
    mock_mgr = MagicMock()
    iface = ms.MediaPlayer2PlayerInterface(mock_mgr)

    assert iface.PlaybackStatus == "Stopped"

    iface.Play()
    assert iface.PlaybackStatus == "Playing"
    mock_mgr.dispatch_control.assert_called_with("play")

    iface.Pause()
    assert iface.PlaybackStatus == "Paused"
    mock_mgr.dispatch_control.assert_called_with("pause")

    iface.Next()
    mock_mgr.dispatch_control.assert_called_with("next")

    iface.Previous()
    mock_mgr.dispatch_control.assert_called_with("previous")


def test_mpris_update_metadata():
    mock_mgr = MagicMock()
    iface = ms.MediaPlayer2PlayerInterface(mock_mgr)

    with patch.object(iface, "emit_properties_changed") as mock_emit:
        iface.update_metadata("Daft Punk", "Get Lucky", "Random Access Memories", 248.0)

    meta = iface.Metadata
    assert meta["xesam:artist"].value == ["Daft Punk"]
    assert meta["xesam:title"].value == "Get Lucky"
    assert meta["xesam:album"].value == "Random Access Memories"
    assert meta["mpris:length"].value == 248000000


def test_mpris_service_dispatch_control():
    service = ms.KaraokeMprisService()

    with patch("karaoke.mpris_service.try_chrome_cdp_navigate") as mock_cdp, \
         patch.object(service, "_publish_event") as mock_pub:
        service.dispatch_control("play_pause")

        mock_cdp.assert_called_once()
        mock_pub.assert_called_with("control.player", {"action": "play_pause"})
