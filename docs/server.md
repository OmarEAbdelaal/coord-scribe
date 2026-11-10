# Running the AI on a company server

By default every PC runs its own models. With one GPU server inside the company network, staff PCs only capture audio and show results; speech recognition and minutes run on the server. Nothing goes to the internet.

```
 Staff PC (Coord desktop)                Company GPU server (LAN only)
 ├─ capture, captions UI      ─────►     ├─ Speech: OpenAI-compatible faster-whisper server  :8000
 ├─ redaction, library, MCP   ─────►     └─ Minutes + translation: Ollama                    :11434
 └─ meeting data stays in ~/Coordy
```

## Server

Recommended hardware for about 5–10 simultaneous meetings: one 24 GB NVIDIA GPU (large-v3 uses about 4–5 GB, qwen3:14b about 10–12 GB with a 32k context), 32 GB RAM, SSD. More concurrent meetings need a second GPU or a smaller minutes model.

**Minutes and translation (Ollama).** Install Ollama on the server, then:

```bash
ollama pull qwen3:14b
```

Make it listen on the network by setting `OLLAMA_HOST=0.0.0.0:11434` for the Ollama service, and set `OLLAMA_KEEP_ALIVE=-1` so the model stays loaded between meetings.

**Speech.** Any server that implements `POST /v1/audio/transcriptions` with `response_format=verbose_json` works. [Speaches](https://github.com/speaches-ai/speaches) (faster-whisper behind an OpenAI-compatible API) is the reference choice; follow its documentation for the current GPU container image and for installing `Systran/faster-whisper-large-v3`. Coord sends 16 kHz mono WAV, the model name, the optional language and the AECO glossary as the prompt.

**Network.** Ollama has no authentication of its own. Allow ports 11434 and 8000 only from the office network/VPN in the server firewall, or put both behind an internal reverse proxy with TLS. If the speech server requires an API key, set `COORDY_SERVER_KEY` in each user's Windows environment; it is sent as a bearer token and never written to disk.

## Each PC

Settings › **AI engine**:

| Field | Example |
| --- | --- |
| Minutes AI server | `http://10.0.0.20:11434` |
| Speech server | `http://10.0.0.20:8000` |
| Speech server model | `Systran/faster-whisper-large-v3` |

The sidebar then shows *Company server* for both and whether the minutes model is reachable. Coord resolves the address and refuses anything that is not loopback or a private network range, so a typo cannot send meeting audio to a public host. The same can be set for the CLI and MCP server in `config.toml` (`[whisper] server`, `[llm] host`, `[privacy] allow_remote_llm = true`).

## What still runs on the PC

- Audio capture, the browser guest, redaction and the meeting library.
- Speaker detection (pyannote), if installed. The speech server API returns text and timestamps only; server-side speaker detection needs a dedicated endpoint and is listed in the [roadmap](roadmap.md).
- Meeting data. Each user's `~/Coordy` is separate; a shared company library is a roadmap item.

## Not yet verified

The client is covered by automated tests against a simulated server. It has not been run against a live Speaches/Ollama deployment in this repository's CI; do one end-to-end check with a short bilingual recording before rolling out.
