# Hermes Voice Call with Discord

> 雙向 Discord 語音 Bot — 聽你講嘢（**Qwen3-ASR**）→ AI 回應（**Hermes Agent** 有齊 tools + skills）→ 語音播放（**edge-tts** 廣東話）

[![License: MIT](https://img.shields.io/badge/Licence-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](pyproject.toml)
[![Discord](https://img.shields.io/badge/discord.py-2.7-blue?logo=discord)](https://github.com/Rapptz/discord.py)

## 流程

```
你（講嘢）──→ Discord 語音頻道 ──→ VAD（WebRTC）──→ 語句偵測
                                                           │
                                                   ┌──────▼──────┐
                                                   │ Qwen3-ASR   │
                                                   │（ComfyUI）   │
                                                   └──────┬──────┘
                                                          │
                                                   ┌──────▼──────┐
                                                   │ Hermes AI   │
                                                   │ Agent       │
                                                   │（tools+     │
                                                   │  skills）    │
                                                   └──────┬──────┘
                                                          │
你（聽到）←── Discord 語音頻道 ←── edge-tts ←── 文字回覆
```

## 功能

- 🎤 **語音聆聽** — WebRTC VAD 偵測講嘢起點同終點
- 🧠 **Hermes AI Agent** — 完整工具存取，同打字對話一樣
- 🗣️ **自動 TTS 語音播放** — Agent 回覆自動用 edge-tts 廣東話讀出
- 🔇 **語音指令** — 講「stop / 閉嘴 / 收聲 / 收皮」停播
- 📝 **文字記錄** — 語音辨識結果 Post 到 Discord 文字頻道
- 🔊 **排隊播放** — 檔案順序播放，唔會疊聲
- 📎 **Unix socket 介面** — 外部程式可經 `play.sock` 注入音訊

## 快速開始

```bash
# 1. 安裝依賴
pip install -r requirements.txt

# 2. 設定環境變數
cp .env.example .env
# 填入 DISCORD_BOT_TOKEN, COMFYUI_URL

# 3. 執行
python3 discord_voice_bot_v4.py --guild 你的伺服器ID --channel "語音頻道名"
```

### Discord 指令

| 指令 | 說明 |
|------|------|
| `!join` | 加入語音頻道 |
| `!leave` | 離開語音頻道 |
| `!listen` | 開始聆聽 |
| `!stoplisten` | 停止聆聽 |
| `!vc_on` / `!vc_off` / `!vc_toggle` | 開關 TTS 播放 |
| `!set_text_channel` | 設定轉錄文字頻道 |
| `!vc_debug` | 顯示連線及設定狀態 |

### 語音指令（直接講）

| 短語 | 效果 |
|------|------|
| "stop" / "閉嘴" / "收聲" / "收皮" | 停止當前 TTS 播放 |

## 設定選項（環境變數）

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DISCORD_BOT_TOKEN` | — | Discord Bot Token **（必填）** |
| `COMFYUI_URL` | — | ComfyUI 伺服器網址 **（必填）** |
| `HERMES_HOME` | `~/.hermes` | Hermes config 目錄（LLM key 放喺度） |
| `SILENCE_TIMEOUT_MS` | `800` | 靜音幾耐當一句完結（毫秒） |
| `PADDING_DURATION_MS` | `300` | 講嘢開始前保留嘅音訊（毫秒） |
| `VAD_AGGRESSIVENESS` | `2` | VAD 敏感度（0-3） |
| `MIN_UTTERANCE_DURATION_MS` | `1200` | 最短語句長度（毫秒） |
| `MIN_UTTERANCE_RMS` | `400` | 音量門檻 — 細過就 skip |
| `TRANSCRIPT_CHANNEL_ID` | — | 轉錄文字頻道 ID |

## For AI Agents

呢個專案設計俾其他 AI coding agent 使用同擴展。

### CLI 用法

```bash
# 啟動語音 Bot
python3 discord_voice_bot_v4.py --guild 123456789 --channel "語音頻道"

# Audio queue: 寫 WAV 檔案去 ~/.hermes/voice_queue/
# 或者經 Unix socket：
echo '{"file": "/path/to/audio.wav"}' | nc -U ~/.hermes/voice_queue/play.sock
```

### Python 匯入

```python
# VAD 語句偵測
from discord_voice_bot_v4 import VADUtteranceSink, start_listening, stop_listening

# TTS 前處理
from discord_voice_bot_v4 import _preprocess_tts
clean = _preprocess_tts("你的 **文字** 內容")
```

### 輸出格式

- 音訊：16-bit mono WAV @ 48kHz
- 轉錄文字：純文字，存於 `~/voice_transcripts/`
- Log：有 emoji 標記嘅 timestamp stdout

## 專案結構

```
hermes-voice-call-with-discord/
├── discord_voice_bot_v4.py   # 主 bot 程式（1330+ 行）
├── .env.example              # 環境變數範本
├── pyproject.toml            # Python 專案設定
├── README.md                 # 英文說明
├── README.zh-HK.md           # 中文說明（呢個）
├── CHANGELOG.md              # 版本記錄
├── CONTRIBUTING.md           # 貢獻指南
├── LICENSE                   # MIT
├── .github/workflows/ci.yml  # CI 流程
├── tests/                    # Pytest 測試
└── examples/                 # 使用範例
```

## License

MIT © nkyang10
