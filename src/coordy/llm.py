"""Minimal client for a local Ollama server."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import httpx

from .config import LLMSettings, PrivacySettings

log = logging.getLogger(__name__)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


class LLMError(RuntimeError):
    pass


class RemoteLLMBlocked(LLMError):
    pass


def is_local(host: str) -> bool:
    return (urlparse(host).hostname or "") in LOCAL_HOSTS


class Ollama:
    def __init__(self, settings: LLMSettings, privacy: PrivacySettings, transport: httpx.BaseTransport | None = None):
        if not is_local(settings.host) and not privacy.allow_remote_llm:
            raise RemoteLLMBlocked(
                f"LLM host {settings.host} is not on this computer. Set privacy.allow_remote_llm = true to allow it."
            )
        self.settings = settings
        self._client = httpx.Client(base_url=settings.host, timeout=settings.timeout_seconds, transport=transport)

    def version(self) -> str:
        return self._client.get("/api/version", timeout=5).json().get("version", "")

    def models(self) -> list[str]:
        return [m["name"] for m in self._client.get("/api/tags", timeout=10).json().get("models", [])]

    def has_model(self, name: str | None = None) -> bool:
        name = name or self.settings.model
        wanted = name if ":" in name else f"{name}:latest"
        return wanted in self.models()

    def pull(self, name: str | None = None, progress=None) -> None:
        with self._client.stream("POST", "/api/pull", json={"model": name or self.settings.model}, timeout=None) as r:
            for line in r.iter_lines():
                if line and progress:
                    progress(json.loads(line))

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        """Chat with structured output: the reply is forced to match `schema`."""
        s = self.settings
        body = {
            "model": s.model,
            "stream": False,
            "format": schema,
            "think": s.think,
            "options": {"num_ctx": s.num_ctx, "temperature": s.temperature},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        r = self._client.post("/api/chat", json=body)
        if r.status_code == 400 and "think" in r.text.lower():
            # Older Ollama or a model without a thinking switch.
            body.pop("think")
            r = self._client.post("/api/chat", json=body)
        if r.status_code == 404:
            raise LLMError(f"Model {s.model} is not installed. Run: ollama pull {s.model}")
        if r.status_code != 200:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        content = r.json().get("message", {}).get("content", "")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON: {content[:200]}") from exc
