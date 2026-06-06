# Usage Examples

## Basic Start

```bash
# Join a specific voice channel
python3 discord_voice_bot_v4.py --guild 123456789 --channel "General"

# With a text channel for transcripts (ID from Discord Developer Mode)
python3 discord_voice_bot_v4.py --guild 123456789 --channel "General" --text-channel 987654321
```

## Environment Setup

```bash
# Required: create .env file with at least:
echo 'DISCORD_BOT_TOKEN=your_bot_token_here
COMFYUI_URL=http://your-comfyui-server:8000' > .env
```

## Inject Audio via Unix Socket

```bash
# Generate TTS audio externally, then inject to voice channel
edge-tts --voice zh-HK-WanLungNeural --text "你好" --write-media /tmp/hello.mp3
ffmpeg -y -i /tmp/hello.mp3 -ar 48000 -ac 1 -sample_fmt s16 /tmp/hello.wav

# Inject via socket
echo '{"file": "/tmp/hello.wav"}' | nc -U ~/.hermes/voice_queue/play.sock
```

## Run as System Service

```bash
# Using screen/tmux
screen -dmS voice-bot python3 discord_voice_bot_v4.py --guild 123456789 --channel "Voice"

# Or use systemd:
cat > /etc/systemd/system/hermes-voice-bot.service << EOF
[Unit]
Description=Hermes Voice Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/hermes-voice-call-with-discord
EnvironmentFile=/home/ubuntu/hermes-voice-call-with-discord/.env
ExecStart=/usr/bin/python3 /home/ubuntu/hermes-voice-call-with-discord/discord_voice_bot_v4.py --guild 123456789 --channel "Voice"
Restart=always

[Install]
WantedBy=multi-user.target
EOF
```

## Debugging

```bash
# Check live logs
tail -f /tmp/voicebot_v4.log

# Test raw audio capture (3 seconds)
# In Discord: !test_capture 3

# Check connection status
# In Discord: !vc_debug
```
