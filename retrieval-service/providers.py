"""
Adapters for external (non-self-hosted) LLM providers.

Every adapter exposes a stream(...) and complete(...) function with the same
signature so main.py can dispatch on backend name without branching on
provider shape itself.

OpenAI, DeepSeek, Google AI Studio (Gemini), and xAI's Grok all speak the same
OpenAI chat-completions shape — DeepSeek and Grok are natively OpenAI-compatible,
and Google AI Studio exposes an official OpenAI-compatibility endpoint
(generativelanguage.googleapis.com/v1beta/openai/) specifically so existing
OpenAI-shaped clients work unchanged. So all four share one adapter,
parameterized only by base URL.

Anthropic's Messages API is the one genuine outlier — system prompt is a
separate top-level field, not a "system"-role message, and it has its own SSE
event stream. Its adapter translates both directions so the rest of the
pipeline (citations, stats footer, Employee Portal parsing) never has to know
which backend answered.

Only translates plain-text chat turns — no tool use, images, or multi-block
content. That's all EKAP's retrieval-augmented chat needs today.
"""
import json
import os

import httpx
from cryptography.fernet import Fernet, InvalidToken

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 4096
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=5.0)

# Native OpenAI-compatible chat-completions endpoints, keyed by provider name.
OPENAI_COMPATIBLE_BASE_URLS = {
    "openai":   "https://api.openai.com/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/v1/chat/completions",
    "google":   "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "grok":     "https://api.x.ai/v1/chat/completions",
}


# ── Secret encryption ───────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    key = os.getenv("SECRETS_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "SECRETS_ENCRYPTION_KEY is not set — required to store or read "
            "external LLM provider API keys. Set it in .env and restart."
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "Could not decrypt the stored API key — SECRETS_ENCRYPTION_KEY may "
            "have changed since it was saved. Re-enter the key on the Settings page."
        ) from e


# ── OpenAI-compatible (OpenAI, DeepSeek, Google AI Studio, Grok) ────────────────

def _openai_messages(system_prompt: str, history: list[dict]) -> list[dict]:
    return [{"role": "system", "content": system_prompt}, *history]


async def _stream_openai_compatible(base_url: str, provider: str, model: str, system_prompt: str, history: list[dict], api_key: str):
    payload = {"model": model, "messages": _openai_messages(system_prompt, history), "stream": True}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream("POST", base_url, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode(errors="replace")[:500]
                raise RuntimeError(f"{provider} error {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if line:
                    yield line + "\n\n"


async def _complete_openai_compatible(base_url: str, provider: str, model: str, system_prompt: str, history: list[dict], api_key: str) -> str:
    payload = {"model": model, "messages": _openai_messages(system_prompt, history), "stream": False}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(base_url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"{provider} error {resp.status_code}: {resp.text[:500]}")
        return resp.json()["choices"][0]["message"]["content"]


def _make_openai_compatible_adapters(provider: str, base_url: str):
    async def stream(model: str, system_prompt: str, history: list[dict], api_key: str):
        async for chunk in _stream_openai_compatible(base_url, provider, model, system_prompt, history, api_key):
            yield chunk

    async def complete(model: str, system_prompt: str, history: list[dict], api_key: str) -> str:
        return await _complete_openai_compatible(base_url, provider, model, system_prompt, history, api_key)

    return stream, complete


# ── Anthropic ────────────────────────────────────────────────────────────────────

def _anthropic_headers(api_key: str) -> dict:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _anthropic_payload(model: str, system_prompt: str, history: list[dict], stream: bool) -> dict:
    return {
        "model": model,
        "system": system_prompt,
        "messages": [{"role": m["role"], "content": m["content"]} for m in history],
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "stream": stream,
    }


async def stream_anthropic(model: str, system_prompt: str, history: list[dict], api_key: str):
    """Translates Anthropic's SSE event stream into OpenAI chat.completion.chunk
    lines, so the caller's downstream parsing never has to branch on backend."""
    payload = _anthropic_payload(model, system_prompt, history, stream=True)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream(
            "POST", "https://api.anthropic.com/v1/messages", json=payload, headers=_anthropic_headers(api_key)
        ) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode(errors="replace")[:500]
                raise RuntimeError(f"Anthropic error {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                try:
                    evt = json.loads(line[len("data: "):])
                except ValueError:
                    continue
                chunk = _anthropic_event_to_openai_chunk(evt)
                if chunk is not None:
                    yield f"data: {json.dumps(chunk)}\n\n"
    yield "data: [DONE]\n\n"


def _anthropic_event_to_openai_chunk(evt: dict) -> dict | None:
    etype = evt.get("type")
    if etype == "content_block_delta" and evt.get("delta", {}).get("type") == "text_delta":
        return {"choices": [{"index": 0, "delta": {"content": evt["delta"]["text"]}, "finish_reason": None}]}
    if etype == "message_stop":
        return {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return None


async def complete_anthropic(model: str, system_prompt: str, history: list[dict], api_key: str) -> str:
    payload = _anthropic_payload(model, system_prompt, history, stream=False)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post("https://api.anthropic.com/v1/messages", json=payload, headers=_anthropic_headers(api_key))
        resp.raise_for_status()
        data = resp.json()
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


# ── Dispatch ─────────────────────────────────────────────────────────────────────

STREAM_ADAPTERS: dict = {"anthropic": stream_anthropic}
COMPLETE_ADAPTERS: dict = {"anthropic": complete_anthropic}
for _provider, _base_url in OPENAI_COMPATIBLE_BASE_URLS.items():
    _stream, _complete = _make_openai_compatible_adapters(_provider, _base_url)
    STREAM_ADAPTERS[_provider] = _stream
    COMPLETE_ADAPTERS[_provider] = _complete
