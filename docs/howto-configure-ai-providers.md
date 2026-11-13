# How-To: Configure AI Providers (AI DJ chat)

The AI DJ chat in the unified dashboard talks to a language model to answer
questions ("what's the vibe of this track?"), suggest songs, and hype the
crowd. By default it runs entirely locally against **Ollama** — no signup, no
API key, no cost. You can additionally configure cloud providers (Claude,
GPT, Gemini) if you want a different model's voice or capability, and switch
between them live from the dashboard.

This guide covers where keys live, what each setting actually does, and the
gaps worth knowing about before you rely on it.

---

## Zero-config default: Ollama

Nothing to do here. `karaoke-dashboard.service` talks to a local Ollama
instance (`OLLAMA_URL`, default `http://127.0.0.1:11434`) and lets you pick
any model you've pulled locally (`qwen3:latest`, `qwen3:1.7b`, etc.) from the
model dropdown in the AI DJ dock header. This is the only provider that
works out of the box and the only one with no per-token cost.

## Adding a cloud provider

Each cloud provider needs one environment variable — its API key — set in
the project's `.env` file (`/home/tina/karaoke/.env`, loaded once at process
startup by `karaoke.config`'s `.env` loader):

```bash
# /home/tina/karaoke/.env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

Only add the ones you actually want to use. A provider with no key configured
simply doesn't show up as available — nothing else breaks, and Ollama keeps
working regardless.

**Important: `.env` is read once at startup, not on every request.** After
adding or changing a key you must restart the dashboard for it to take
effect:

```bash
systemctl --user restart karaoke-dashboard.service
```

### Where to get each key

- **OpenAI (GPT)** — <https://platform.openai.com/api-keys>. Default model
  is `gpt-4o-mini` (cheap); `gpt-4o` is also offered in the dropdown.
- **Google (Gemini)** — <https://aistudio.google.com/app/apikey>. Has a free
  tier. Default model is `gemini-flash-latest`.
- **Anthropic (Claude)** — <https://console.anthropic.com/settings/keys>.
  **Note:** this requires a real API console account with its own billing,
  separate from a claude.ai chat subscription (Pro/Team/Max). A claude.ai
  subscription has no API access and no OAuth bridge into it — don't expect
  a Team/Pro login to work here. Default model is
  `claude-3-5-haiku-latest`.

### Verifying a key took effect

```bash
curl -s http://127.0.0.1:8900/api/dj/providers | python3 -m json.tool
```

Each provider reports `"available": true/false`. **`available: true` means
the key is present and non-empty — it does not mean the provider is
currently answering successfully.** A configured key can still fail at
request time (invalid/revoked key, hitting the provider's own rate limit,
or — as happened during testing — the provider's API returning `503
UNAVAILABLE` for a specific model under high demand). If a provider shows
`available: true` in the dashboard but chat requests to it error out, check
the error text returned in the chat bubble; it's almost always the
upstream API's own message passed straight through, not a karaoke bug.

## Switching providers from the dashboard

The AI DJ dock header shows a provider dropdown (only listing providers
that are actually configured) next to the model dropdown. Picking a
provider there is per-browser-session (stored in `localStorage`) and takes
effect immediately — no restart needed, since this is a per-request choice,
unlike adding a new key.

## Switching providers via the API directly

Useful for scripting or testing a provider without touching the UI:

```bash
curl -N -X POST http://127.0.0.1:8900/api/dj/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Suggest an upbeat 80s song", "provider": "gemini", "model": "gemini-flash-latest"}'
```

`provider` defaults to `"ollama"` if omitted. `model` defaults to that
provider's own default model if omitted.

## Overriding model lists or base URLs (optional)

Each provider's offered model list and endpoint come from a small built-in
table in `karaoke.ai_providers`. To change which models are offered, or
point a provider at a custom/compatible endpoint (an OpenAI-compatible
proxy, a different region, etc.) without touching code, create
`config/ai_providers.json` at the project root:

```json
{
  "providers": [
    {
      "id": "openai",
      "label": "GPT (OpenAI)",
      "kind": "openai",
      "default_model": "gpt-4o",
      "key_env": "OPENAI_API_KEY",
      "base_url_default": "https://api.openai.com/v1",
      "models": ["gpt-4o", "gpt-4o-mini"]
    }
  ]
}
```

**Never put a raw API key in this file** — only the *name* of the
environment variable holding it (`key_env`), exactly like the example
above. The file only overrides non-secret metadata; keys always come from
the environment. The path defaults to `config/ai_providers.json` relative
to the repo root, or wherever `KARAOKE_AI_PROVIDERS_FILE` points.

## Checking which provider is actually configured right now

```bash
curl -s http://127.0.0.1:8900/api/dj/providers | python3 -m json.tool
```

## Related: pushing an imported playlist through full analysis

Unrelated to providers, but adjacent new surface: `karaoke-ytmusic-playlist
--import-playlist <ID> --enqueue` imports every track of a YouTube Music
playlist into the library (as before) and additionally publishes each track
that isn't already fully analyzed (key/BPM, mood, chords, CLAP vectors) to
the real post-processing pipeline. Tracks the library already has fully
analyzed are skipped, reported as `already_analyzed` in the summary, so
re-running `--enqueue` on an overlapping playlist doesn't waste downloads.
