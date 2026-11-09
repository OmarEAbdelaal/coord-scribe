# Windows desktop phase one

Coord 0.2 adds a native Windows meeting workspace using the APG Brand Kit. It captures the selected Windows speaker output and, optionally, your microphone; transcribes Arabic, English and mixed speech locally; optionally translates text using local Ollama; and extracts decisions, actions, risks and questions. Completed redacted minutes can be delivered through a Teams Workflow webhook or SMTP.

This is an initial implementation. Automated behavior is covered by tests with fake audio and model backends. Windows hardware capture, provider guest admission, real recognition quality and installer execution still need the acceptance checks below. No speech model guarantees every word, especially with overlapping speakers, dialects or poor audio.

## Run from source on Windows 11 x64

Install Git, Microsoft Edge and [uv](https://docs.astral.sh/uv/getting-started/installation/). For summaries and translation, install [Ollama](https://ollama.com/download/windows), start it and run `ollama pull qwen3:14b` (about 9 GB). A smaller local model can be selected in `~/Coordy/config.toml` on lower-memory PCs.

```powershell
git clone https://github.com/OmarEAbdelaal/coord-scribe.git
cd coord-scribe
uv venv --python 3.12
uv pip install -e ".[desktop]"
.venv\Scripts\coordy init
.venv\Scripts\coordy setup-models
.venv\Scripts\coordy-desktop.exe
```

The model setup command downloads the configured Whisper model (default `large-v3`, about 3 GB) from Hugging Face and the configured LLM through local Ollama. It fails with a nonzero exit code if a required download fails. Model files live in the Hugging Face/Ollama cache, not Git. Internet is needed for installation and the initial downloads, not local inference. Allow Hugging Face and its `*.hf.co` weight-download hosts if using a restricted network.

Speaker labeling is optional. Without the diarization extra, live transcripts and minutes work without speaker identification. To install NVIDIA support and speaker identification from source:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Desktop
```

Accept the [pyannote model terms](https://huggingface.co/pyannote/speaker-diarization-community-1) and securely configure `HF_TOKEN`, then rerun `coordy setup-models`. The packaged application omits optional PyTorch/pyannote and CUDA DLLs to keep its distribution manageable. It supports CPU inference; the source installation is recommended for NVIDIA and speaker labeling. CPU large-v3 can be substantially slower than real time. The live queue is bounded; if it falls behind, the complete recording is transcribed again after Stop, rather than silently omitting speech.

## Appearance

The interface follows the Windows light/dark appearance and updates when the system theme changes. Settings, confirmation dialogs, dropdowns and disabled controls share the same APG palette. The original APG logo is displayed with its aspect ratio preserved.

## Models and settings

The first launch creates `~/Coordy` (override with `COORDY_HOME`). Edit `config.toml` for Whisper model/device, language, glossary and Ollama model. Arabic/English code switching uses faster-whisper's multilingual mode. Inference uses existing pretrained models; training is not required. Real-time translation is text-only via the local LLM, selected as Arabic or English in desktop settings. It does not speak into the meeting. Translations are saved beside the redacted transcript.

Use **Download models** in the UI or `coordy setup-models`. A missing Ollama service, incomplete download or denied Hugging Face access is reported rather than marked successful. Once models are downloaded, set `privacy.offline = true` to prevent further Hugging Face downloads. The full `coordy doctor` also checks optional deployment features such as Claude Desktop and GPU availability.

Edit `aliases.toml` before sharing minutes. Only the redacted view is shown and delivered by the desktop app. Unknown names not present in the aliases file can remain visible; review the result. Raw audio and original transcripts remain local under `audio/` and `raw/`.

## Capture and detection

1. Open **Settings → Meeting audio output** and select the speaker or headset used by Teams/Zoom/your browser. Click **Refresh audio devices** after connecting or switching devices. The Windows default output can differ from a meeting app’s communications output. Select **Microphone input** separately and choose whether to **Include my microphone**. Computer sound is always captured, including when the microphone is disabled. Stop and start capture after changing devices.
2. Click **Start capture**, or opt into **Detect meetings and presentations**.
3. Live text appears in roughly ten-second chunks. Final minutes are produced after **Stop and summarise**. Audio is streamed to disk while recording.
4. Review the transcript, translation and minutes. Export Markdown or send to your configured destination.

Detection checks active Windows audio sessions for Teams, Zoom and Webex. Browser sessions also need a meeting-like window title. PowerPoint slide-show windows are detected separately. Detection requires two positive polls and uses a 90-second stop grace period (configurable) to tolerate short silences. This is a heuristic: it cannot reliably distinguish every meeting from notifications, inactive browser tabs or presentations. Continuous audio activity can delay stopping; use Stop manually. Concurrent meetings are recorded together. Automatic capture and delivery are disabled by default, and the app stays visibly open while recording or processing. Tell participants that recording is taking place.

The status bar has separate **Computer sound** and **Mic** meters, measured before mixing. Hover over each meter to see its selected device. If computer sound stays silent for ten seconds, the app suggests checking the output selection. Normal meeting silence can also trigger that notice. An unplugged saved device produces an error rather than silently switching to another endpoint.

To diagnose a mic-only recording: play meeting audio, stop capture, choose that exact output in Settings, turn **Include my microphone** off, and start capture again. The **Computer sound** meter should move. If it does not, check the meeting application's speaker/output setting and Windows **System → Sound → Volume mixer**. Bluetooth devices can switch between stereo and hands-free endpoints when a call starts; refresh/select the endpoint in use during the meeting. After confirming computer audio, re-enable the microphone if needed.

Capture uses WASAPI loopback and includes **all audio** on that speaker device, not just a particular meeting. Keep unrelated applications quiet and use headphones to reduce duplicate mic pickup. The microphone and speaker streams are mixed; this does not provide per-participant tracks. Hardware clock drift and echo need real-device testing. Recordings are retained on processing errors and can be imported again after correcting the model/configuration issue.

If the **Computer sound** meter moves but captions stay empty, audio is reaching capture; inspect the caption processing message for Whisper loading, CPU fallback, transcription, or Ollama translation. Processing time depends on hardware and may exceed the ten-second capture window. The desktop records model-loading/fallback diagnostics in `~/Coordy/logs/desktop.log`. Select a failed meeting and open **Minutes and actions** to see its stored error. A quiet Mic meter must be checked separately against the selected input, hardware mute and Windows desktop-app microphone permissions.

## Live captions panel

Open **Live captions** in the main workspace. The panel shows the latest original speech in large text, with its timestamp and detected language. Use **Translate to** to choose **Off · original speech**, **Arabic**, or **English**. This selector remains available during capture; a changed language applies to the next speech segment. Starting capture automatically opens this panel.

Speech arrives in approximately ten-second chunks through local Whisper. Translation uses local Ollama and may arrive later, depending on your hardware. Late translations from a previous language or caption are ignored by the live panel. A translation error appears in the panel while recording and original captions continue. Final summaries still require a working Ollama service. Full transcript and translation history remain in their separate tabs, and saved translations include timestamps and the selected language.

## Join as a separate participant

Paste a Teams, Google Meet or Zoom HTTPS invitation, choose a visible name and click **Join as guest**. A separate Microsoft Edge window opens in a fresh browser context. Coord attempts common English guest-name/join controls and never bypasses a host lobby, authentication, CAPTCHA or organizational guest policy. Complete provider-specific steps in that window when needed. The participant is listen-only: microphone and camera permissions are denied. You hear its audio through this PC, and the Windows capture path transcribes it.

When a recognized Leave control becomes visible, the app reports the guest as joined and starts capture if idle. If the provider UI differs, use Start capture manually after verifying that the guest is admitted. **Leave guest** closes that guest browser and stops capture. Joining the same meeting from your personal client and this guest may duplicate audio; mute one playback source. No participant joins during installation or automated tests.

Supported provider domains: `teams.microsoft.com`, `teams.live.com`, `meet.google.com`, `zoom.us` and its subdomains. Desktop-only meetings, meeting links on organization redirect domains, localized join controls and providers which prohibit web guests require manual joining or a future provider adapter. This browser guest is not a Microsoft Graph/Azure or Zoom server-side media bot. A managed bot that joins when your PC is off requires a separately registered, hosted provider service and account permissions; it is outside this desktop implementation.

## Teams and email

No live messages are sent by tests or installation. Configure a destination before enabling automatic delivery.

**Teams:** create a Teams Workflow using the webhook trigger that posts an Adaptive Card to your chosen chat/channel. Use a template that accepts the message/attachments payload and the appropriate trigger authentication policy. Store its HTTPS URL in your Windows user environment as `COORDY_TEAMS_WEBHOOK` and restart Coord. Supported Workflow hosts end in `.logic.azure.com` or `.environment.api.powerplatform.com`; legacy Office connector webhooks are not supported. The app posts an Adaptive Card with the redacted Markdown. Summaries over 22 KB are rejected rather than truncated. A successful webhook response confirms acceptance by the workflow, not downstream Teams delivery; inspect Workflow run history on failure.

**Email:** enter SMTP host, STARTTLS port (usually 587), username, sender and recipients in Settings. Store the password or app password in `COORDY_SMTP_PASSWORD` in your Windows user environment and restart Coord. Only authenticated TLS connections are used. The adapter supports SMTP password authentication, not Microsoft Graph mail or Exchange OAuth. Exchange Online tenants that disable SMTP AUTH need a compatible relay/provider or a future OAuth adapter.

Credentials are not written to `desktop.json` or the delivery database. Do not commit webhook URLs, passwords, tokens or meeting recordings. The application re-redacts minutes immediately before export and delivery. Automatic delivery uses the selected channel only after successful final minutes generation. A failed send leaves the meeting and minutes intact.

`delivery.db` records delivery identities and results. Identical content to the same destination is not sent twice, including after restart. Failures require an explicit **Retry delivery** after checking whether the message arrived. Interrupted `sending` attempts can be manually retried after ten minutes. Remote services cannot guarantee exactly-once delivery when a connection fails after accepting a message; the app does not silently retry those requests.

## Build the Windows installer

Install [Inno Setup 6](https://jrsoftware.org/isinfo.php). Run on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-desktop.ps1
```

This runs tests, builds the native app with PyInstaller and produces `dist/installer/Coord-Setup-0.2.0-x64.exe`. The portable app is `dist/Coord/Coord.exe`. The installer runs per user without administrator privileges, adds shortcuts, and preserves meeting data on uninstall. It includes application libraries and fonts, not Whisper/LLM weights or Ollama/Edge. It is unsigned; code signing needs your organization's signing certificate.

The **Windows desktop installer** GitHub Actions workflow can be run manually after it is available on the repository's default branch; tags named `desktop-v*` also trigger builds. It uploads the installer and portable app as a workflow artifact. It tests the frozen UI startup but cannot test real audio devices or hosted meetings. An artifact is not a published release.

## Development and acceptance checks

```bash
uv pip install -e ".[dev,desktop,package]"
uv run --no-sync pytest -q
uv build --wheel
```

On Linux, file processing and the interface can be tested; live capture/detection require Windows. `QT_QPA_PLATFORM=offscreen coordy-desktop --smoke-test` constructs and exits the actual UI without joining or recording.

Before daily use, validate on the target Windows PC:

- Install the generated installer, start Ollama and download models. Check CPU/GPU operation with a short known recording.
- Play a bilingual test recording; capture with microphone off and on, then verify both sources, timestamps, Arabic rendering, overlapping speech and final summaries.
- Try detection start/stop, extended silence, PowerPoint presentation, manual override, device failure and a long meeting slower than real-time transcription.
- Admit the named browser guest to test Teams, Meet and Zoom meetings. Verify camera/microphone remain off, then check guest leaving and capture stopping. Test host-denied admission separately.
- Send a sample redacted summary to a test Teams workflow and test mailbox; verify arrival, retries and duplicate protection.
- Restart the app and verify meeting history, transcripts, translations and minutes persist.

The prepared plugin has not been supplied or integrated. Existing MCP integration continues to work and can be used separately by Claude Desktop.
