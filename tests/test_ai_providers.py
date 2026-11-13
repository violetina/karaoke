"""Tests for the multi-provider AI DJ registry (karaoke.ai_providers)."""
from __future__ import annotations

import pytest

from karaoke import ai_providers


def test_list_providers_reports_ollama_always_available():
    providers = {p["id"]: p for p in ai_providers.list_providers()}
    assert "ollama" in providers
    assert providers["ollama"]["needs_key"] is False
    assert providers["ollama"]["available"] is True


def test_list_providers_reports_cloud_providers_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    providers = {p["id"]: p for p in ai_providers.list_providers()}
    for pid in ("anthropic", "openai", "gemini"):
        assert providers[pid]["needs_key"] is True
        assert providers[pid]["available"] is False


def test_provider_becomes_available_once_key_env_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    providers = {p["id"]: p for p in ai_providers.list_providers()}
    assert providers["anthropic"]["available"] is True


def test_get_provider_unknown_raises():
    with pytest.raises(ValueError):
        ai_providers.get_provider("not-a-real-provider")


def test_base_url_for_respects_env_override(monkeypatch):
    p = ai_providers.get_provider("ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://example.invalid:9999")
    assert ai_providers.base_url_for(p) == "http://example.invalid:9999"


def test_base_url_for_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    p = ai_providers.get_provider("ollama")
    assert ai_providers.base_url_for(p) == "http://127.0.0.1:11434"


@pytest.mark.anyio
async def test_stream_chat_raises_providererror_when_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ai_providers.ProviderError):
        async for _ in ai_providers.stream_chat("anthropic", None, "sys", "hello"):
            pass


@pytest.mark.anyio
async def test_stream_chat_unknown_provider_raises():
    with pytest.raises(ValueError):
        async for _ in ai_providers.stream_chat("not-a-real-provider", None, "sys", "hello"):
            pass
