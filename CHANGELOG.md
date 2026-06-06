# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-06-06

### Added
- Initial release from Hermes Voice Bot v4
- Bidirectional Discord voice: listen (VAD + Qwen3-ASR) → respond (Hermes AI Agent) → speak (edge-tts)
- WebRTC VAD-based utterance endpoint detection with configurable silence timeout
- Qwen3-ASR 1.7B via ComfyUI for transcription
- Hermes AI Agent integration with full tool & skills access
- Auto-TTS to voice channel for agent responses (Cantonese via edge-tts zh-HK-WanLungNeural)
- Voice commands: "stop / 閉嘴 / 收聲 / 收皮" to stop playback
- Volume gate (RMS threshold) to ignore quiet speakerphone noise
- Duration gate (1200ms minimum) to skip short utterances
- Soft limiter to prevent audio clipping
- Unix socket interface for external audio injection
- Queue-based sequential playback (no overlap)
- Raw audio capture for diagnostics (`!test_capture`)
- State persistence across restarts
- DAVE protocol support for Discord encrypted voice
