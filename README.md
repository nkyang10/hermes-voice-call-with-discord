# Hermes Voice Call with Discord

> Bidirectional Discord voice bot — listens via **Qwen3-ASR**, responds via **Hermes AI Agent** (DeepSeek v4 Flash with full tool access), speaks via **edge-tts**.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](pyproject.toml)
[![Discord](https://img.shields.io/badge/discord.py-2.7-blue?logo=discord)](https://github.com/Rapptz/discord.py)

## Architecture

```
You (speak) ──→ Discord Voice Channel ──→ VAD (WebRTC) ──→ Utterance Detection
                                                                    │
                                                            ┌──────▼──────┐
                                                            │ Qwen3-ASR   │
                                                            │ (ComfyUI)   │
                                                            └──────┬──────┘
                                                                   │
                                                            ┌──────▼──────┐
                                                            │ Hermes AI   │
                                                            │ Agent       │
                                                            │ (tools+     │
                                                            │  skills)    │
                                                            └──────┬──────┘
                                                                   │
You (hear) ←── Discord Voice Channel ←── edge-tts ←── Text Reply
```

## Features

- 🎤 **Voice listening** — WebRTC VAD-based utterance endpoint detection
- 🧠 **Hermes AI Agent** — Full tool & skills access, same as chat
- 🗣️ **Auto-TTS to VC** — Agent replies auto-played as Cantonese voice (edge-tts)
- 🔇 **Voice commands** — Say "stop" / "閉嘴" / "收聲" to stop playback
- 📝 **Transcriptions** — Posted to a Discord text channel for readability
- 🔊 **Queue-based playback** — Files play sequentially, no overlap
- 📎 **Unix socket interface** — External processes can inject audio via `play.sock`

## Prerequisites

- Python 3.10+
- A **Discord Bot Token** with voice intents enabled
- **ComfyUI** server running with [Qwen3-ASR custom nodes](https://github.com/flybirdxx/ComfyUI-Qwen3-TTS)
- **DeepSeek API key** (or any OpenAI-compatible LLM provider)
- [discord.py](https://github.com/Rapptz/discord.py) + [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv)
- [edge-tts](https://github.com/rany2/edge-tts) — free TTS via Microsoft Edge
- [webrtcvad](https://github.com/wiseman/py-webrtcvad) — voice activity detection

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set up environment
cp .env.example .env
# Fill in DISCORD_BOT_TOKEN, COMFYUI_URL, DEEPSEEK_API_KEY

# 3. Run the bot
python3 discord_voice_bot_v4.py --guild YOUR_GUILD_ID --channel "Voice Channel Name"
```

### Discord Commands

| Command | Description |
|---------|-------------|
| `!join` | Join voice channel |
| `!leave` | Leave voice channel |
| `!listen` | Start VAD listening |
| `!stoplisten` | Stop listening |
| `!vc_on` / `!vc_off` / `!vc_toggle` | Toggle TTS playback |
| `!set_text_channel` | Set current text channel for transcripts |
| `!vc_debug` | Show connection & config status |

### Voice Commands (speak these)

| Phrase | Effect |
|--------|--------|
| "stop" / "閉嘴" / "收聲" / "收皮" | Stop current TTS playback |

## Configuration (via environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | — | Discord bot token **(required)** |
| `COMFYUI_URL` | — | ComfyUI server URL for Qwen3-ASR **(required)** |
| `DEEPSEEK_API_KEY` | — | DeepSeek API key for Hermes agent |
| `SILENCE_TIMEOUT_MS` | `800` | Silence duration to end utterance (ms) |
| `PADDING_DURATION_MS` | `300` | Audio padding before utterance start (ms) |
| `VAD_AGGRESSIVENESS` | `2` | WebRTC VAD mode (0-3, higher = more aggressive) |
| `MIN_UTTERANCE_DURATION_MS` | `1200` | Minimum utterance length to process (ms) |
| `MIN_UTTERANCE_RMS` | `400` | Volume gate — skip quieter audio |
| `TRANSCRIPT_CHANNEL_ID` | — | Discord channel ID for ASR transcripts |

## For AI Agents

This project is designed to be used and extended by other AI coding agents.

### CLI Usage

```bash
# Start the voice bot
python3 discord_voice_bot_v4.py --guild 123456789 --channel "Voice Channel"

# Audio queue interface: write WAV files to ~/.hermes/voice_queue/
# Or send via Unix socket:
echo '{"file": "/path/to/audio.wav"}' | nc -U ~/.hermes/voice_queue/play.sock
```

### Python Imports

The script's core components can be imported:

```python
# VAD utterance detector
from discord_voice_bot_v4 import VADUtteranceSink, start_listening, stop_listening

# TTS preprocessing
from discord_voice_bot_v4 import _preprocess_tts
clean = _preprocess_tts("Your **text** here")
```

### Output Format

- Audio files: 16-bit mono WAV @ 48kHz
- Transcripts: plain text, saved to `~/voice_transcripts/`
- Logs: timestamped stdout with emoji indicators for each stage

## How It Works

1. **VAD Sink** (`VADUtteranceSink`): Receives stereo PCM from Discord, downmixes to mono, runs WebRTC VAD on 20ms frames. Uses a 15-frame ring buffer and 3-speech-frame trigger to detect utterance start. Silence timeout (800ms) ends the utterance.

2. **ASR** (`asr_via_comfyui`): Uploads the WAV to ComfyUI, runs Qwen3-ASR 1.7B (auto language detection), polls for result. Returns transcription text.

3. **Hermes Agent** (`inline_respond`): Filters non-speech ASR output (code, paths, URLs), then calls `AIAgent.chat()` with full tool access. Posts reply to text channel.

4. **Auto-TTS** (`on_message`): Bot messages in the configured text channel trigger edge-tts → ffmpeg → WAV → voice queue → sequential playback.

5. **Playback** (`watch_queue_fast` + `play_file`): Polls queue dir every 50ms, plays sequentially (blocks while `is_playing()`), auto-deletes played files.

## Dependencies

- `discord.py` — Discord API (MIT)
- `discord-ext-voice-recv` — Voice receive support (MIT)
- `edge-tts` — Microsoft Edge TTS (GPL-3.0)
- `webrtcvad` — WebRTC Voice Activity Detection (BSD)
- `ffmpeg` — Audio conversion (GPL)
- ComfyUI + [Qwen3-ASR custom nodes](https://github.com/flybirdxx/ComfyUI-Qwen3-TTS)

## Project Structure

```
hermes-voice-call-with-discord/
├── discord_voice_bot_v4.py   # Main bot script (1330+ lines)
├── .env.example              # Environment variable template
├── pyproject.toml            # Python project config
├── README.md                 # This file
├── README.zh-HK.md           # Traditional Chinese version
├── CHANGELOG.md              # Version history
├── CONTRIBUTING.md           # Contributor guide
├── LICENSE                   # MIT
├── .github/workflows/ci.yml  # CI pipeline
├── tests/                    # Pytest tests
└── examples/                 # Usage examples
```

## License

MIT © nkyang10

## Acknowledgments

- [Qwen3-ASR](https://github.com/QwenLM/Qwen3) by Alibaba
- [discord-ext-voice-recv](https://github.com/imayhaveborkedit/discord-ext-voice-recv) by imayhaveborkedit
- [Hermes Agent](https://hermes-agent.nousresearch.com) by Nous Research
