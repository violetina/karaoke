"""Multi-provider LLM registry for the AI DJ chat.

The DJ booth (`dashboard_api.dj_chat_endpoint`) used to be hardcoded to a
local Ollama instance. This module adds a small provider registry so the
booth can also be pointed at Anthropic (Claude), OpenAI (GPT) or Google
(Gemini), while keeping Ollama as the zero-config local default.

Configuration follows this project's existing convention (see
`karaoke.config`): everything is environment-variable driven, optionally
loaded from a `.env` file at the project root, no secrets committed to the
repo. Non-secret provider metadata (which models to offer, base URLs) can be
overridden without code changes via an optional JSON file pointed to by
`KARAOKE_AI_PROVIDERS_FILE` (default: `config/ai_providers.json` at the repo
root, if present) -- the file lists providers but never contains API keys
themselves, only the *name* of the env var holding each key.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from .config import settings as _settings  # noqa: F401  (ensures .env is loaded)


@dataclass(frozen=True)
class ProviderDef:
    id: str
    label: str
    kind: str  # "ollama" | "anthropic" | "openai" | "gemini"
    default_model: str
    key_env: Optional[str] = None  # None => no key needed (e.g. local Ollama)
    base_url_env: Optional[str] = None
    base_url_default: Optional[str] = None
    models: List[str] = field(default_factory=list)


_BUILTIN_PROVIDERS: List[ProviderDef] = [
    ProviderDef(
        id="ollama",
        label="Ollama (local)",
        kind="ollama",
        default_model=os.environ.get("KARAOKE_DJ_MODEL", "qwen3:latest"),
        key_env=None,
        base_url_env="OLLAMA_URL",
        base_url_default="http://127.0.0.1:11434",
    ),
    ProviderDef(
        id="anthropic",
        label="Claude (Anthropic)",
        kind="anthropic",
        default_model="claude-3-5-haiku-latest",
        key_env="ANTHROPIC_API_KEY",
        base_url_env="ANTHROPIC_BASE_URL",
        base_url_default="https://api.anthropic.com",
        models=["claude-3-5-haiku-latest", "claude-3-5-sonnet-latest"],
    ),
    ProviderDef(
        id="openai",
        label="GPT (OpenAI)",
        kind="openai",
        default_model="gpt-4o-mini",
        key_env="OPENAI_API_KEY",
        base_url_env="OPENAI_BASE_URL",
        base_url_default="https://api.openai.com/v1",
        models=["gpt-4o-mini", "gpt-4o"],
    ),
    ProviderDef(
        id="gemini",
        label="Gemini (Google)",
        kind="gemini",
        default_model="gemini-flash-latest",
        key_env="GEMINI_API_KEY",
        base_url_env="GEMINI_BASE_URL",
        base_url_default="https://generativelanguage.googleapis.com/v1beta",
        models=["gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-pro-latest"],
    ),
]


def _config_file_path() -> Optional[Path]:
    override = os.environ.get("KARAOKE_AI_PROVIDERS_FILE")
    if override:
        return Path(override).expanduser()
    # project root is three levels above this file (src/karaoke/ai_providers.py)
    parents = Path(__file__).resolve().parents
    if len(parents) > 3:
        candidate = parents[3] / "config" / "ai_providers.json"
        if candidate.is_file():
            return candidate
    return None


def _load_provider_overrides() -> Dict[str, ProviderDef]:
    """Load optional non-secret provider overrides/additions from JSON.

    Expected shape: {"providers": [{"id": ..., "label": ..., "kind": ...,
    "default_model": ..., "key_env": ..., "base_url_env": ...,
    "base_url_default": ..., "models": [...]}, ...]}
    Never put raw API keys in this file -- only the *name* of the env var.
    """
    path = _config_file_path()
    if not path:
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    out: Dict[str, ProviderDef] = {}
    for p in data.get("providers", []):
        try:
            out[p["id"]] = ProviderDef(
                id=p["id"],
                label=p.get("label", p["id"]),
                kind=p.get("kind", p["id"]),
                default_model=p.get("default_model", ""),
                key_env=p.get("key_env"),
                base_url_env=p.get("base_url_env"),
                base_url_default=p.get("base_url_default"),
                models=p.get("models", []),
            )
        except Exception:
            continue
    return out


def _all_providers() -> Dict[str, ProviderDef]:
    reg = {p.id: p for p in _BUILTIN_PROVIDERS}
    reg.update(_load_provider_overrides())
    return reg


def provider_available(p: ProviderDef) -> bool:
    """True if the provider is usable right now (key present, or no key needed)."""
    if p.key_env is None:
        return True
    return bool(os.environ.get(p.key_env, "").strip())


def base_url_for(p: ProviderDef) -> str:
    if p.base_url_env:
        return os.environ.get(p.base_url_env, p.base_url_default or "")
    return p.base_url_default or ""


def list_providers() -> List[Dict[str, Any]]:
    """Describe every registered provider for the dashboard UI.

    Never includes raw key values, only whether a key is configured.
    """
    out = []
    for p in _all_providers().values():
        out.append({
            "id": p.id,
            "label": p.label,
            "kind": p.kind,
            "default_model": p.default_model,
            "models": p.models,
            "needs_key": p.key_env is not None,
            "key_env": p.key_env,
            "available": provider_available(p),
        })
    return out


def get_provider(provider_id: str) -> ProviderDef:
    reg = _all_providers()
    if provider_id not in reg:
        raise ValueError(f"Unknown AI provider: {provider_id!r}")
    return reg[provider_id]


class ProviderError(RuntimeError):
    pass


async def stream_chat(
    provider_id: str,
    model: Optional[str],
    system_prompt: str,
    user_msg: str,
    timeout: float = 60.0,
) -> AsyncGenerator[str, None]:
    """Stream plain-text tokens from the chosen provider.

    Yields incremental text chunks (not full messages). Raises ProviderError
    for configuration problems (missing key, unknown provider) before any
    network call is made, so callers can show a clean error immediately.
    """
    p = get_provider(provider_id)
    if not provider_available(p):
        raise ProviderError(
            f"Provider '{p.label}' is not configured: set the {p.key_env} "
            f"environment variable (e.g. in .env) to enable it."
        )
    chosen_model = (model or p.default_model or "").strip() or p.default_model

    if p.kind == "ollama":
        async for chunk in _stream_ollama(p, chosen_model, system_prompt, user_msg, timeout):
            yield chunk
    elif p.kind == "anthropic":
        async for chunk in _stream_anthropic(p, chosen_model, system_prompt, user_msg, timeout):
            yield chunk
    elif p.kind == "openai":
        async for chunk in _stream_openai(p, chosen_model, system_prompt, user_msg, timeout):
            yield chunk
    elif p.kind == "gemini":
        async for chunk in _stream_gemini(p, chosen_model, system_prompt, user_msg, timeout):
            yield chunk
    else:
        raise ProviderError(f"No streaming implementation for provider kind {p.kind!r}")


async def _stream_ollama(p: ProviderDef, model: str, system_prompt: str, user_msg: str, timeout: float):
    url = f"{base_url_for(p)}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = item.get("message", {}).get("content", "")
                if token:
                    yield token


async def _stream_anthropic(p: ProviderDef, model: str, system_prompt: str, user_msg: str, timeout: float):
    url = f"{base_url_for(p)}/v1/messages"
    headers = {
        "x-api-key": os.environ.get(p.key_env or "", ""),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_msg}],
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ProviderError(f"Anthropic error {resp.status_code}: {body[:300]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    item = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if item.get("type") == "content_block_delta":
                    token = item.get("delta", {}).get("text", "")
                    if token:
                        yield token


async def _stream_openai(p: ProviderDef, model: str, system_prompt: str, user_msg: str, timeout: float):
    url = f"{base_url_for(p)}/chat/completions"
    headers = {
        "Authorization": f"Bearer {os.environ.get(p.key_env or '', '')}",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": True,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ProviderError(f"OpenAI error {resp.status_code}: {body[:300]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    item = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = item.get("choices", [])
                if choices:
                    token = choices[0].get("delta", {}).get("content", "")
                    if token:
                        yield token


async def _stream_gemini(p: ProviderDef, model: str, system_prompt: str, user_msg: str, timeout: float):
    key = os.environ.get(p.key_env or "", "")
    url = f"{base_url_for(p)}/models/{model}:streamGenerateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", url, params={"key": key, "alt": "sse"}, json=payload
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise ProviderError(f"Gemini error {resp.status_code}: {body[:300]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):].strip()
                if not data:
                    continue
                try:
                    item = json.loads(data)
                except json.JSONDecodeError:
                    continue
                candidates = item.get("candidates", [])
                if candidates:
                    for part in candidates[0].get("content", {}).get("parts", []):
                        token = part.get("text", "")
                        if token:
                            yield token
