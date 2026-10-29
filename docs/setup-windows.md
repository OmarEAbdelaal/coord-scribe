# Setting up Coord on Windows 11 (RTX 5080)

Allow about one hour. Most of it is downloads (roughly 10 GB in total).

## 1. Prerequisites (once)

| What | How | Check |
|---|---|---|
| NVIDIA driver (recent, for the RTX 50 series) | NVIDIA app, or nvidia.com/drivers | `nvidia-smi` in PowerShell shows the RTX 5080 |
| Git | `winget install Git.Git` | `git --version` |
| Ollama | `winget install Ollama.Ollama` or ollama.com/download | `ollama --version`; the tray icon is running |
| Claude Desktop | claude.ai/download | Signed in |
| OBS Studio (recording) | `winget install OBSProject.OBSStudio` | Opens |

You do **not** need to install Python, CUDA Toolkit or FFmpeg. The installer gets Python through `uv`, and the CUDA libraries come with PyTorch and pip packages.

## 2. Install Coord

```powershell
cd $env:USERPROFILE
git clone https://github.com/OmarEAbdelaal/coord-scribe.git
cd coord-scribe
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

The script installs `uv`, creates `.venv` with Python 3.12, installs PyTorch with CUDA 12.8+ (needed for Blackwell GPUs), installs Coord with speaker detection, creates `%USERPROFILE%\Coordy`, and runs `coordy doctor`.

## 3. Speaker model access (free, once)

pyannote's speaker model is free, but you must accept its terms before downloading it:

1. Create a free account at huggingface.co.
2. Open https://huggingface.co/pyannote/speaker-diarization-community-1 and accept the conditions.
3. Settings → Access Tokens → create a **Read** token.
4. Paste it into `%USERPROFILE%\Coordy\config.toml`:
   ```toml
   [diarization]
   hf_token = "hf_..."
   ```

The token is only used to download the model. Your audio never leaves your PC.

## 4. Download the models

```powershell
.venv\Scripts\coordy setup-models
```

This downloads Whisper large-v3 (about 3 GB), the speaker model (small) and `qwen3:14b` through Ollama (about 9 GB). When it finishes, set `offline = true` under `[privacy]` in `config.toml` and remove the `hf_token`.

## 5. Names to hide

Edit `%USERPROFILE%\Coordy\aliases.toml`. Add every team member, consultant, client, contractor and project name, including Arabic spellings:

```toml
[[person]]
name = "Ahmed Hassan"
code = "P02"
role = "MEP Coordinator, Consultant X"
variants = ["Ahmed", "أحمد", "احمد", "م. أحمد"]
```

After any change, run `coordy redact --all` and then `coordy minutes <meeting>`. To check for names you missed, run `coordy suggest-aliases <meeting>`.

## 6. Connect Claude Desktop

```powershell
.venv\Scripts\coordy claude-config --write
```

Quit Claude Desktop fully (right-click the tray icon → Quit), then reopen it. You should see **coordy** under the tools / connectors menu. If you prefer to edit the config by hand, run `coordy claude-config` without `--write`, then paste the snippet in Claude Desktop → Settings → Developer → Edit Config.

## 7. Recording with OBS (online meetings)

1. Settings → Output → Recording: set **Recording Path** to `%USERPROFILE%\Coordy\inbox`. Keep the format at `mkv`.
2. Settings → Video: set Output (scaled) resolution to the lowest option. Only the audio matters.
3. Audio Mixer: keep **Desktop Audio** (the other participants) and **Mic/Aux** (you).
4. Start Recording when the meeting starts and Stop when it ends.

For Teams or Zoom cloud recordings, download the `.mp4` into the inbox. For phone recordings (`.m4a`), copy them into the inbox.

Remember to tell participants the meeting is recorded.

## 8. First test

```powershell
.venv\Scripts\coordy doctor
.venv\Scripts\coordy process "$env:USERPROFILE\Coordy\inbox\<your file>" --project PRJ01 -v
```

Then check:

- `Coordy\redacted\<meeting>.md`: is the transcript accurate? Are names hidden?
- `Coordy\minutes\<meeting>.md`: are the decisions and actions right?
- In Claude Desktop: *"List my Coord meetings and show the minutes of the latest one."*

## Optional: process recordings automatically

Run `coordy watch` in a terminal. To start it at logon, create a basic task in Task Scheduler with:

- Trigger: *At log on*
- Program: `%USERPROFILE%\coord-scribe\.venv\Scripts\coordy.exe`
- Arguments: `watch`

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor`: no CUDA GPU for faster-whisper | Update the NVIDIA driver; re-run `install.ps1`. Coord still works on CPU (slower) |
| `cublas64_12.dll` / `cudnn` not found | Re-run `install.ps1`. It installs the CUDA 12 runtime |
| Ollama not reachable | Start Ollama from the Start menu; `ollama list` should show `qwen3:14b` |
| Model is slow or runs out of VRAM | In `config.toml`, set `num_ctx = 16384` or `model = "qwen3:8b"` |
| Arabic words wrong | Add terms to `glossary.txt`; force `language = "ar"` for Arabic-only meetings |
| Speakers merged or split | Set `min_speakers` / `max_speakers` in `config.toml` |
| Claude does not show coordy | Quit Claude Desktop from the tray icon and reopen it; check `Coordy\logs\mcp.log` |
