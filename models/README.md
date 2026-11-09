# Local model catalog

`manifest.json` documents the default pretrained models. Weights are downloaded through `coordy setup-models` or the desktop's **Download models** action. They are deliberately excluded from source control and the Windows installer: speech and LLM weights total approximately 12 GB. No training is necessary.

Use `~/Coordy/config.toml` to select a model supported by faster-whisper or a local CTranslate2 model directory. The same settings select the local Ollama model. Download with normal TLS and Hugging Face/Ollama verification enabled. Model access requires internet during setup; diarization additionally requires accepting the upstream terms. See [desktop setup](../docs/desktop.md).

Cloud onboarding currently blocks Hugging Face downloads until the saved network-domain additions are applied. Code installation and mock tests do not establish that real model weights are present or recognition accuracy is validated.
