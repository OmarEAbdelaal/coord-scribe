"""Speech-to-text on a company GPU server (OpenAI-compatible /v1/audio/transcriptions).

Works with self-hosted faster-whisper servers such as Speaches. Audio leaves this
PC, so only loopback or private-network hosts are accepted unless
privacy.allow_remote_llm is set.
"""

from __future__ import annotations

import io
import ipaddress
import os
import socket
import wave
from urllib.parse import urlparse

import httpx

from .transcribe import SAMPLE_RATE, Segment

LANGUAGE_NAMES = {"arabic": "ar", "english": "en"}


class ServerBlocked(RuntimeError):
    pass


def is_private_host(url: str, resolve=socket.getaddrinfo) -> bool:
    """True when every address behind `url` is loopback or on a private network."""
    host = urlparse(url).hostname or ""
    if not host:
        return False
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in resolve(host, None)]
        except OSError:
            return False
    return bool(addresses) and all(a.is_private or a.is_loopback or a.is_link_local for a in addresses)


def wav_bytes(audio) -> bytes:
    import numpy as np

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buffer.getvalue()


class RemoteTranscriber:
    """Same interface as transcribe.Transcriber, backed by a server on the company network."""

    def __init__(self, settings, glossary: str = "", status=None, *, allow_remote: bool = False,
                 transport: httpx.BaseTransport | None = None):
        parsed = urlparse(settings.server)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ServerBlocked(f"Speech server must be an http(s) URL, got: {settings.server}")
        if not allow_remote and not is_private_host(settings.server):
            raise ServerBlocked(
                f"Speech server {settings.server} is not on this computer or a private network. "
                "Set privacy.allow_remote_llm = true to allow it."
            )
        self.settings = settings
        self.glossary = glossary
        self.status = status or (lambda message: None)
        self.device = "server"
        key = os.environ.get("COORDY_SERVER_KEY", "")
        self._client = httpx.Client(base_url=settings.server.rstrip("/"), timeout=settings.server_timeout,
                                    transport=transport,
                                    headers={"Authorization": f"Bearer {key}"} if key else {})

    def close(self):
        self._client.close()

    def transcribe(self, audio, progress=None) -> list[Segment]:
        s = self.settings
        data = {"model": s.server_model or s.model, "response_format": "verbose_json", "temperature": "0"}
        if s.language:
            data["language"] = s.language
        if self.glossary:
            data["prompt"] = self.glossary[:800]
        try:
            response = self._client.post("/v1/audio/transcriptions", data=data,
                                         files={"file": ("audio.wav", wav_bytes(audio), "audio/wav")})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Speech server {s.server} is unreachable: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"Speech server error {response.status_code}: {response.text[:200]}")
        body = response.json()
        language = str(body.get("language") or "").lower()
        language = LANGUAGE_NAMES.get(language, language)
        out = []
        for item in body.get("segments") or []:
            text = str(item.get("text", "")).strip()
            if text:
                out.append(Segment(float(item["start"]), float(item["end"]), text, language=language))
        if not out and str(body.get("text", "")).strip():
            out.append(Segment(0.0, len(audio) / SAMPLE_RATE, body["text"].strip(), language=language))
        if progress:
            progress(1.0)
        return out
