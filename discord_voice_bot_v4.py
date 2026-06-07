#!/usr/bin/env python3
"""
Discord Voice Bot v4 — Bidirectional: Listen + Speak
- Listens to VC audio with VAD-based utterance segmentation
- Sends segments to Qwen3-ASR for transcription
- Posts transcriptions to a Discord text channel
- Also plays TTS audio from queue/socket (v3 playback preserved)

Usage: python3 discord_voice_bot_v4.py --guild GUILD_ID [--channel NAME] [--text-channel ID]
"""

import asyncio
import array
import collections
import io
import json
import os
import re

from dotenv import load_dotenv
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path

import discord
from discord.ext import commands, voice_recv
import webrtcvad

# Ensure opus is loaded (discord.py requires this for voice)
discord.opus._load_default()

# ── DAVE + Opus decode monkey-patches ──
import traceback as _tb
from discord.ext.voice_recv.opus import PacketDecoder as _PacketDecoder, VoiceData

# DAVE support check
try:
    from davey import MediaType
    has_dave = True
except ImportError:
    has_dave = False

# ── Patch 1: DAVE decrypt in _process_packet ──
_original_process_packet = _PacketDecoder._process_packet
def _dave_process_packet(self, packet):
    """Monkey-patch: add DAVE decryption before Opus decode."""
    pcm = None
    member = self._get_cached_member()

    if member is None:
        try:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
        except Exception:
            pass
        member = self._get_cached_member()

    # DAVE decrypt
    if has_dave and not packet.is_silence() and packet.decrypted_data is not None:
        try:
            vc = self.sink.voice_client
            if vc and vc._connection and vc._connection.dave_session is not None and vc._connection.dave_session.ready:
                packet.decrypted_data = bytes(vc._connection.dave_session.decrypt(
                    member.id if member else 0,
                    MediaType.audio,
                    bytes(packet.decrypted_data),
                ))
        except Exception:
            # Use packet (with encrypted data → opus will try FEC concealment)
            pass

    if not self.sink.wants_opus():
        try:
            packet, pcm = self._decode_packet(packet)
        except Exception:
            pcm = b""

    data = VoiceData(packet, member, pcm=pcm)
    self._last_seq = packet.sequence
    self._last_ts = packet.timestamp
    return data
_PacketDecoder._process_packet = _dave_process_packet

# ── Patch 2: Enable passthrough mode on dave_session ──
_original_pd_init = _PacketDecoder.__init__
def _dave_pd_init(self, router, ssrc):
    _original_pd_init(self, router, ssrc)
    try:
        self.vc = self.sink.voice_client
        if has_dave and self.vc and self.vc._connection and self.vc._connection.dave_session is not None:
            self.vc._connection.dave_session.set_passthrough_mode(True, 10)
    except Exception:
        pass
_PacketDecoder.__init__ = _dave_pd_init

# ── Patch 3: RTP padding strip + Opus error handling in _decode_packet ──
_original_decode_packet = _PacketDecoder._decode_packet
_decode_error_count = [0]
_OPUS_STEREO_FRAME_BYTES = 3840  # 960 samples × 2 channels × 2 bytes

def _safe_decode_packet(self, packet):
    # Strip RTP padding before Opus decode (fixes corrupted stream errors)
    if getattr(packet, 'padding', False) and packet.decrypted_data:
        pad_len = packet.decrypted_data[-1]
        if 0 < pad_len <= len(packet.decrypted_data):
            packet.decrypted_data = packet.decrypted_data[:-pad_len]

    try:
        result = _original_decode_packet(self, packet)
        if _decode_error_count[0] > 0:
            log(f"✅ Opus decode recovered (had {_decode_error_count[0]} errors)")
            _decode_error_count[0] = 0
        return result
    except Exception as e:
        _decode_error_count[0] += 1
        if _decode_error_count[0] <= 3:
            log(f"⚠️ Opus decode #{_decode_error_count[0]}: {type(e).__name__}: {e}")
        elif _decode_error_count[0] % 100 == 4:
            log(f"⚠️ Opus decode error count: {_decode_error_count[0]} (still failing)")
        # Reset decoder + return silence for this frame
        import discord.opus as _opus
        self._decoder = _opus.Decoder()
        return packet, b"\x00" * _OPUS_STEREO_FRAME_BYTES
_PacketDecoder._decode_packet = _safe_decode_packet

# ── Patch 4: Skip None-source packets in router ──
from discord.ext.voice_recv.router import PacketRouter as _PacketRouter
_original_do_run = _PacketRouter._do_run
def _patched_do_run(self):
    import traceback as _tb2
    try:
        while not self._end_thread.is_set():
            self.waiter.wait()
            with self._lock:
                for decoder in self.waiter.items:
                    data = decoder.pop_data()
                    if data is not None and data.source is not None:
                        self.sink.write(data.source, data)
    except Exception:
        log(f"❌ Router error:\n{_tb2.format_exc()}")
_PacketRouter._do_run = _patched_do_run

# ── END MONKEY-PATCHES ──

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
ENV_PATH = HERMES_HOME / ".env"

# ── Load scripts/.env for runtime config (COMFYUI_URL, etc.) ──
_scripts_dotenv = Path(__file__).parent / ".env"
load_dotenv(_scripts_dotenv)

def log(*a, **kw):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, **kw, flush=True)

def read_token():
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""

TOKEN = read_token()
QUEUE_DIR = HERMES_HOME / "voice_queue"
SOCK_PATH = QUEUE_DIR / "play.sock"
TMP_DIR = HERMES_HOME / "voice_queue_tmp"
SEGMENT_DIR = HERMES_HOME / "voice_segments"
TRANSCRIPT_DIR = HERMES_HOME / "voice_transcripts"

# ── Config (env only) ──
COMFYUI_URL = os.environ["COMFYUI_URL"]

# ── Telegram config (from .env) ──
# Note: bot cannot send directly (TELEGRAM_HOME_CHANNEL == bot's own ID).
# Instead, WAVs are saved to VOICE_FORWARD_DIR for Hermes cron to pick up.
VOICE_FORWARD_DIR = HERMES_HOME / "voice_forward"

# ── Config (overridable via env) ──
SAMPLE_RATE = 48000
FRAME_DURATION_MS = 20  # 20ms frames for VAD
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 960 samples = 1920 bytes

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

state = {
    "vc": None,
    "channel_id": None,
    "text_channel_id": None,
    "enabled": True,
    "toggle_file": QUEUE_DIR / ".vctoggle",
    "listening": False,
}

# ── ASR queue (thread-safe) ──
asr_queue = collections.deque()
asr_queue_lock = threading.Lock()

# ── Playback (v3 preserved) ──────────────────────────────────

async def is_enabled():
    toggle = state["toggle_file"]
    if toggle.exists():
        state["enabled"] = toggle.read_text().strip() == "1"
    return state["enabled"]

async def set_enabled(val: bool):
    state["enabled"] = val
    state["toggle_file"].write_text("1" if val else "0")
    log(f"{'🔊 Enabled' if val else '🔇 Disabled'} voice playback")

async def play_file(file_path):
    if not state["vc"] or not state["vc"].is_connected():
        log("⚠️ Not connected")
        return False

    fpath = Path(file_path).resolve()
    if not fpath.exists():
        log(f"⚠️ File not found: {fpath}")
        return False

    while state["vc"].is_playing():
        await asyncio.sleep(0.1)

    try:
        source = discord.FFmpegPCMAudio(str(fpath))
        source = discord.PCMVolumeTransformer(source, volume=1.0)
        fname = fpath.name

        def after(err):
            if err:
                log(f"❌ Playback error: {err}")
            try:
                fpath.unlink(missing_ok=True)
            except OSError:
                pass
            log(f"✅ Played: {fname}")

        state["vc"].play(source, after=after)
        log(f"▶️ Playing: {fname}")
        return True
    except Exception as e:
        log(f"❌ Play failed: {e}")
        return False

async def unix_socket_server():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    if SOCK_PATH.exists():
        SOCK_PATH.unlink()

    async def handle_client(reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5)
            if data:
                msg = json.loads(data.decode())
                fpath = msg.get("file", "")
                if fpath:
                    await play_file(fpath)
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            log(f"❌ Socket error: {e}")
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle_client, path=str(SOCK_PATH))
    os.chmod(SOCK_PATH, 0o777)
    log(f"🔌 Socket ready: {SOCK_PATH}")
    async with server:
        await server.serve_forever()

async def watch_queue_fast():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"👀 Watching fast: {QUEUE_DIR}")
    last_files = set()
    while True:
        if state["vc"] and state["vc"].is_connected():
            current = {
                f.name for f in QUEUE_DIR.iterdir()
                if f.suffix.lower() in (".mp3", ".wav", ".ogg", ".opus", ".m4a")
                and f.name != "play.sock"
            }
            new_files = current - last_files
            for fname in sorted(new_files):
                fpath = QUEUE_DIR / fname
                if fpath.exists() and not state["vc"].is_playing():
                    await play_file(str(fpath))
        last_files = {
            f.name for f in QUEUE_DIR.iterdir()
            if f.suffix.lower() in (".mp3", ".wav", ".ogg", ".opus", ".m4a")
            and f.name != "play.sock"
        }
        await asyncio.sleep(0.05)

# ── VAD Utterance Segmenter ──────────────────────────────────

class VADUtteranceSink(voice_recv.AudioSink):
    """
    Custom AudioSink that:
    1. Receives PCM audio from Discord voice
    2. Uses WebRTC VAD + ring buffer to detect utterance boundaries
    3. When an utterance ends (silence timeout), saves it as WAV
    4. Queues for ASR processing
    """

    def __init__(self, *, vad_aggressiveness=2,
                 silence_timeout_ms=800,
                 padding_duration_ms=300,
                 min_utterance_ms=1200,
                 min_utterance_rms=10):
        super().__init__()
        self.vad = webrtcvad.Vad(vad_aggressiveness)
        self.silence_timeout_ms = silence_timeout_ms
        self.padding_duration_ms = padding_duration_ms
        self.min_utterance_ms = min_utterance_ms
        self.min_utterance_rms = min_utterance_rms

        self.num_padding_frames = padding_duration_ms // FRAME_DURATION_MS
        self.num_silence_frames = silence_timeout_ms // FRAME_DURATION_MS

        # Per-user state
        self.user_buffers: dict[int, collections.deque] = {}
        self.user_voiced_frames: dict[int, list] = {}
        self.user_speaking: dict[int, bool] = {}
        self.user_silence_count: dict[int, int] = {}
        self.user_ignore_until: dict[int, float] = {}
        
        # Anti-restart-loop
        self._restart_count = 0
        self._created_at = time.time()

    def wants_opus(self) -> bool:
        return False  # Let voice_recv handle decoding (patched to catch errors)

    # ── Sink event listeners (sync only!) ──
    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member: discord.Member):
        uid = member.id
        log(f"🎤 Speaking START: {member.display_name} ({uid})")
        # If user has an in-progress utterance from a previous burst, flush it now.
        # This lets Discord's multiple VAD bursts accumulate into one full sentence.
        if uid in self.user_speaking and self.user_speaking[uid]:
            self._flush_utterance_if_any(uid)
        # Initialize buffer for this user
        if uid not in self.user_buffers:
            self.user_buffers[uid] = collections.deque(maxlen=self.num_padding_frames)
        self.user_voiced_frames[uid] = []
        self.user_speaking[uid] = False
        self.user_silence_count[uid] = 0

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_stop(self, member: discord.Member):
        uid = member.id
        log(f"🎤 Speaking STOP: {member.display_name} ({uid}) — was_speaking={self.user_speaking.get(uid, False)}")
        # Don't flush on STOP — Discord sends STOP aggressively between speech
        # bursts. The next Speaking START will flush the accumulated utterance.
        pass

    def write(self, user, data: voice_recv.VoiceData):
        """Called for each received audio packet (from a thread, not async)."""
        try:
            self._write(user, data)
        except Exception:
            import traceback
            log(f"❌ write() exception:\n{traceback.format_exc()}")

    def _write(self, user, data: voice_recv.VoiceData):
        if user is None:
            return

        uid = user.id
        pcm = data.pcm
        if not pcm:
            return

        # Downmix stereo → mono before frame processing
        # Opus decoder outputs 48kHz stereo interleaved [L0,R0,L1,R1,...]
        # We MUST downmix here so downstream VAD + WAV use correct sample counts
        if len(pcm) >= 4:
            samples = array.array('h', pcm)
            n_pairs = len(samples) // 2
            mono = array.array('h', [0]) * n_pairs
            for i in range(n_pairs):
                mono[i] = samples[i*2]  # Take Left channel only
            pcm = mono.tobytes()

        # Skip if we're ignoring this user
        if uid in self.user_ignore_until:
            if time.time() < self.user_ignore_until[uid]:
                return
            del self.user_ignore_until[uid]

        # Initialize per-user state
        if uid not in self.user_buffers:
            self.user_buffers[uid] = collections.deque(maxlen=self.num_padding_frames)
        if uid not in self.user_speaking:
            self.user_speaking[uid] = False
            self.user_silence_count[uid] = 0
            self.user_voiced_frames[uid] = []

        # Process PCM in 20ms frames
        offset = 0
        frame_count = 0
        speech_count = 0
        while offset + FRAME_SIZE * 2 <= len(pcm):  # FRAME_SIZE samples × 2 bytes
            frame_bytes = pcm[offset:offset + FRAME_SIZE * 2]
            offset += FRAME_SIZE * 2

            try:
                is_speech = self.vad.is_speech(frame_bytes, SAMPLE_RATE)
            except Exception:
                is_speech = False
            
            frame_count += 1
            if is_speech:
                speech_count += 1

            ring = self.user_buffers[uid]
            was_speaking = self.user_speaking[uid]

            if not was_speaking:
                ring.append((frame_bytes, is_speech))
                num_voiced = sum(1 for _, s in ring if s)
                # Trigger with 3+ speech frames in the window (sensitive enough for real-time)
                if num_voiced >= 3:
                    # Triggered: start of utterance
                    self.user_speaking[uid] = True
                    self.user_silence_count[uid] = 0
                    # Include the buffered frames (with padding)
                    self.user_voiced_frames[uid].extend(fb for fb, _ in ring)
                    ring.clear()
                    log(f"🗣️ Utterance START: user={uid} (voiced={num_voiced}/{self.num_padding_frames} in ring)")
            else:
                # We're in an utterance
                self.user_voiced_frames[uid].append(frame_bytes)
                if is_speech:
                    self.user_silence_count[uid] = 0
                else:
                    self.user_silence_count[uid] += 1

                # Check silence timeout
                if self.user_silence_count[uid] > self.num_silence_frames:
                    # Utterance complete! Trim trailing silence
                    silence_frames = self.user_silence_count[uid]
                    voiced = self.user_voiced_frames[uid]
                    if silence_frames < len(voiced):
                        voiced = voiced[:-silence_frames]
                    self._emit_utterance(uid, voiced)
                    # Reset
                    self.user_speaking[uid] = False
                    self.user_silence_count[uid] = 0
                    self.user_voiced_frames[uid] = []
                    log(f"🗣️ Utterance END: user={uid} ({len(voiced)} frames)")

        # (VAD processing done — see utterance START/END logs for output)

    def _flush_utterance_if_any(self, uid):
        """Force-end any in-progress utterance for a user."""
        if uid in self.user_speaking and self.user_speaking[uid]:
            voiced = self.user_voiced_frames.get(uid, [])
            if voiced:
                self._emit_utterance(uid, voiced)
            self.user_speaking[uid] = False
            self.user_voiced_frames[uid] = []

    def _emit_utterance(self, uid: int, frames: list[bytes]):
        """Save utterance as WAV and queue for ASR."""
        total_ms = len(frames) * FRAME_DURATION_MS
        if total_ms < self.min_utterance_ms:
            log(f"⏭️ Skipping short utterance ({total_ms}ms < {self.min_utterance_ms}ms)")
            return

        # Volume gate: skip quiet utterances (speakerphone background noise)
        pcm_data = b"".join(frames)
        import array as _arr
        import math as _math
        samples = _arr.array('h', pcm_data)
        if self.min_utterance_rms and samples:
            # RMS = sqrt(mean(sample^2)) for each sample
            sum_sq = sum(s * s for s in samples)
            rms = _math.isqrt(sum_sq // len(samples))  # integer RMS
            if rms < self.min_utterance_rms:
                log(f"⏭️ Skipping quiet utterance ({rms} RMS < {self.min_utterance_rms}, {total_ms}ms)")
                return

        SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        wav_path = SEGMENT_DIR / f"utterance_{uid}_{ts}.wav"

        # Apply soft limiter to prevent clipping distortion
        peak = max(abs(s) for s in samples) if samples else 0

        if peak > 24000:  # Near clipping threshold
            # Soft knee limiter: reduce gain smoothly for peaks
            # Target: bring peak down to 20000, scale proportionally
            target_peak = 20000
            # Calculate gain reduction with soft knee
            if peak > target_peak:
                gain = target_peak / peak
                # Apply with 0.5dB soft knee smoothing
                knee_db = 0.5
                knee_ratio = 10 ** (knee_db / 20)
                gain = gain * knee_ratio
                if gain > 1.0:
                    gain = 1.0
                # Apply gain
                for i in range(len(samples)):
                    val = int(samples[i] * gain)
                    # Clamp to valid range
                    if val > 32767:
                        val = 32767
                    elif val < -32768:
                        val = -32768
                    samples[i] = val
                pcm_data = samples.tobytes()
                log(f"🎚️ Soft limiter applied: peak was {peak}, gain={gain:.3f}, now {max(abs(s) for s in samples)}")

        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm_data)

        # Also copy to voice_forward/ for Hermes cron Telegram delivery
        VOICE_FORWARD_DIR.mkdir(parents=True, exist_ok=True)
        fwd_path = VOICE_FORWARD_DIR / wav_path.name
        import shutil
        shutil.copy2(str(wav_path), str(fwd_path))
        log(f"📤 Queued for Telegram forward: {fwd_path.name}")

        dur_s = len(frames) * FRAME_DURATION_MS / 1000
        log(f"💾 Saved: {wav_path.name} ({dur_s:.1f}s, {len(frames)} frames)")

        # Queue for ASR
        with asr_queue_lock:
            asr_queue.append((uid, str(wav_path)))

    def cleanup(self):
        age = time.time() - self._created_at
        log(f"🧹 VAD Sink cleaned up (age={age:.1f}s)")

    def _schedule_restart(self):
        """Restart listening after cleanup."""
        time.sleep(0.5)
        log("🔄 Attempting auto-restart listen...")
        try:
            if state["vc"] and state["vc"].is_connected():
                # Need to fully stop the old reader first
                try:
                    state["vc"].stop_listening()
                except Exception:
                    pass
                asyncio.run_coroutine_threadsafe(_restart_listening(), state["vc"].loop)
        except Exception as e:
            log(f"❌ Auto-restart failed: {e}")

# ── ASR Worker ───────────────────────────────────────────────

def run_asr_worker(loop, bot_ref):
    """
    Runs in a background thread. Picks up WAV files from asr_queue,
    sends to Qwen3-ASR via ComfyUI, and posts transcriptions to Discord.
    """
    log("🤖 ASR worker started")
    while True:
        uid = wav_path = None
        with asr_queue_lock:
            if asr_queue:
                uid, wav_path = asr_queue.popleft()

        if wav_path:
            try:
                log(f"🎙️ ASR processing: {Path(wav_path).name}")
                transcription = asr_via_comfyui(wav_path, uid)
                if transcription:
                    log(f"📝 ASR result: {transcription[:80]}...")
                    asyncio.run_coroutine_threadsafe(
                        _post_transcription(bot_ref, uid, transcription, wav_path),
                        loop,
                    )
                else:
                    log(f"⚠️ ASR returned empty for {Path(wav_path).name}")

                try:
                    Path(wav_path).unlink(missing_ok=True)
                except OSError:
                    pass
            except Exception as e:
                import traceback as _tb2
                log(f"❌ ASR error: {e}\n{_tb2.format_exc()}")
        else:
            time.sleep(0.2)

async def _post_transcription(bot_ref, uid: int, text: str, wav_path: str):
    """Post transcription + trigger auto-respond."""
    text = text.strip()
    if not text:
        return

    # Get member name
    member_name = f"<@{uid}>"
    try:
        if state["vc"] and state["vc"].guild:
            member = state["vc"].guild.get_member(uid)
            if member:
                member_name = member.display_name
    except Exception:
        pass

    # Save transcript to file (always)
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    tsf = int(time.time())
    tpath = TRANSCRIPT_DIR / f"transcript_{tsf}.txt"
    tpath.write_text(f"[{member_name}]\n{text}\n", encoding="utf-8")

    # Post to Discord text channel (if configured) — as clean text
    ch_id = state.get("text_channel_id")
    if ch_id:
        channel = bot_ref.get_channel(int(ch_id))
        if channel:
            try:
                await channel.send(text)
                log(f"📤 ASR posted: {text[:80]}...")
            except Exception as e:
                log(f"❌ Failed to post ASR: {e}")
    else:
        log(f"📝 Transcription (no text channel configured): {text}")

    # Inline auto-respond with DeepSeek v4 flash (same model as Hermes agent)
    # Uses conversation history from file for context
    if text.strip():
        asyncio.create_task(inline_respond(bot_ref, text.strip(), member_name))

# ── Inline Auto-Response: Hermes AI Agent ───────────────────

def is_natural_speech(text: str) -> bool:
    """Check if ASR text sounds like natural conversational speech."""
    t = text.strip()
    if len(t) < 3:
        return False

    # Count CJK characters vs ASCII symbols
    cjk = sum(1 for c in t if '\u4e00' <= c <= '\u9fff')
    ascii_alpha = sum(1 for c in t if c.isascii() and c.isalpha())
    symbol_count = sum(1 for c in t if c in '{}[]=<>|\\/`~!@#$%^&*+')

    # Skip if mostly code characters
    if symbol_count > len(t) * 0.3:
        return False

    # Skip if it looks like a file path
    if t.startswith('/') or t.startswith('./') or t.startswith('~/'):
        return False
    if '/.' in t or '.py' in t or '.js' in t or '.ts' in t:
        return False
    if '://' in t:
        return False

    # Skip if it's mostly numbers and symbols (no CJK, no real words)
    if cjk == 0 and ascii_alpha < 3:
        return False

    return True

# ── Voice Commands ───────────────────────────────────────────
STOP_COMMANDS = {"stop", "閉嘴", "收聲", "閉咀", "收嗲", "收皮"}

def _execute_voice_command(cmd: str) -> str | None:
    """Execute a voice command and return a description, or None if not a command."""
    c = cmd.strip().lower()
    if c in STOP_COMMANDS:
        # Stop current TTS playback only — keep listening + auto-response running
        try:
            if state["vc"] and state["vc"].is_playing():
                state["vc"].stop()
                # Clean up orphaned queue files so they don't pile up
                for f in QUEUE_DIR.iterdir():
                    if f.suffix.lower() in (".wav", ".mp3", ".ogg") and f.name != "play.sock":
                        f.unlink(missing_ok=True)
                log("🔇 Voice: stop playback")
            else:
                log("🔇 Voice: stop command (nothing playing)")
        except Exception as e:
            log(f"⚠️ Stop playback error: {e}")
        return "stop"
    return None

# ── Hermes Agent singleton ────────────────────────────────────
_HERMES_AGENT = None
_HERMES_AGENT_LOCK = threading.Lock()

def _get_hermes_agent():
    """Get or create the shared Hermes AIAgent instance."""
    global _HERMES_AGENT
    if _HERMES_AGENT is not None:
        return _HERMES_AGENT
    with _HERMES_AGENT_LOCK:
        if _HERMES_AGENT is not None:
            return _HERMES_AGENT
        try:
            # Import inside the lock + function to avoid circular/startup issues
            import sys as _sys
            agent_path = str(HERMES_HOME / "hermes-agent")
            if agent_path not in _sys.path:
                _sys.path.insert(0, agent_path)
            from run_agent import AIAgent
            _HERMES_AGENT = AIAgent(
                model="deepseek-v4-flash",
                skip_context_files=True,  # fresh convo each time
                skip_memory=False,        # still load user memory
            )
            log("🤖 Hermes AIAgent initialized")
        except Exception as e:
            import traceback
            log(f"❌ Failed to init Hermes AIAgent: {e}\n{traceback.format_exc()}")
            return None
    return _HERMES_AGENT

async def inline_respond(bot_ref, user_text: str, member_name: str):
    """Auto-respond using Hermes AIAgent (full tool access)."""
    try:
        # Check voice commands first
        cmd = _execute_voice_command(user_text)
        if cmd == "stop":
            return

        # Skip ASR that doesn't sound like natural speech
        if not is_natural_speech(user_text):
            log(f"⏭️ Not natural speech, skipping: {user_text[:40]}...")
            return

        log(f"🤔 Responding via Hermes: {user_text[:50]}...")

        # Post thinking indicator
        ch_id = state.get("text_channel_id")
        channel = bot_ref.get_channel(int(ch_id)) if ch_id else None

        # Get the Hermes agent
        agent = _get_hermes_agent()
        if agent is None:
            log("❌ No Hermes agent available")
            if channel:
                await channel.send("❌ 內部錯誤，Hermes agent 無法啟動")
            return

        # Call Hermes agent (runs in executor to not block event loop)
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(None, agent.chat, user_text)

        if not reply or not reply.strip():
            log("⚠️ Hermes returned empty response")
            return

        log(f"💬 Reply: {reply[:80]}...")

        # Post to channel (on_message auto-TTS handles VC playback)
        if channel:
            await channel.send(reply)

    except Exception as e:
        import traceback
        log(f"❌ Inline respond error: {e}\n{traceback.format_exc()}")


# ── ComfyUI Qwen3-ASR ───────────────────────────────────────

def asr_via_comfyui(wav_path: str, uid: int) -> str:
    """
    Send audio to Qwen3-ASR on ComfyUI, return transcription.
    Uploads file to ComfyUI input directory first.
    """
    import urllib.request
    import urllib.error

    # Upload audio file to ComfyUI
    fname = Path(wav_path).name
    upload_result = subprocess.run(
        ["curl", "-s", "-X", "POST", f"{COMFYUI_URL}/api/upload/image",
         "-F", f"image=@{wav_path}", "-F", "type=input", "-F", "overwrite=True"],
        capture_output=True, text=True, timeout=15,
    )
    if upload_result.returncode != 0 or not upload_result.stdout.strip():
        log(f"❌ ComfyUI upload failed: {upload_result.stderr}")
        return ""
    try:
        upload_info = json.loads(upload_result.stdout)
        uploaded_name = upload_info.get("name", fname)
    except Exception:
        uploaded_name = fname

    # Build ComfyUI ASR workflow (matching the installed Qwen3-ASR custom nodes)
    workflow = {
        "1": {
            "inputs": {
                "repo_id": "Qwen/Qwen3-ASR-1.7B",
                "source": "HuggingFace",
                "precision": "bf16",
                "attention": "auto",
                "forced_aligner": "None",
                "local_model_path": "",
            },
            "class_type": "Qwen3ASRLoader",
            "_meta": {"title": "Qwen3-ASR Loader"},
        },
        "2": {
            "inputs": {
                # STT priority: Cantonese + English (auto-detect between these)
                "language": "auto",
                "context": "",
                "return_timestamps": False,
                "model": ["1", 0],
                "audio": ["4", 0],
            },
            "class_type": "Qwen3ASRTranscribe",
            "_meta": {"title": "Qwen3-ASR Transcribe"},
        },
        "4": {
            "inputs": {"audio": uploaded_name},
            "class_type": "LoadAudio",
            "_meta": {"title": "Load Audio"},
        },
        "5": {
            "inputs": {"source": ["2", 0]},
            "class_type": "PreviewAny",
            "_meta": {"title": "Preview Text"},
        },
    }

    prompt_data = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(
        f"{COMFYUI_URL}/api/prompt",
        data=prompt_data,
        headers={"Content-Type": "application/json"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            log("❌ No prompt_id from ComfyUI")
            return ""
    except Exception as e:
        log(f"❌ ComfyUI submit failed: {e}")
        return ""

    # Poll for result
    for attempt in range(45):
        time.sleep(0.5)
        try:
            hist_req = urllib.request.Request(f"{COMFYUI_URL}/api/history/{prompt_id}")
            hist_resp = urllib.request.urlopen(hist_req, timeout=10)
            hist = json.loads(hist_resp.read())
            if prompt_id in hist:
                status = hist[prompt_id]["status"]["status_str"]
                if status == "success":
                    outputs = hist[prompt_id]["outputs"]
                    for node_id, node_output in outputs.items():
                        text = node_output.get("text", "")
                        if isinstance(text, list):
                            text = "".join(text)
                        if text.strip():
                            try:
                                Path(wav_path).unlink(missing_ok=True)
                            except OSError:
                                pass
                            return text.strip()
                    return ""
                elif status == "error":
                    err = hist[prompt_id]["status"].get("messages", [["unknown"]])
                    log(f"❌ ASR error: {err}")
                    return ""
        except Exception:
            pass

    log(f"⚠️ ASR timed out for {Path(wav_path).name}")
    return ""

# ── Bot Events ───────────────────────────────────────────────

@bot.event
async def on_message(message):
    """Log messages, process commands, and auto-TTS agent responses to VC."""
    if message.guild:
        log(f"📩 [{message.channel.name}] {message.author.display_name}: {message.content[:100]}")

    await bot.process_commands(message)

    # Auto-TTS: if the message is from bot.user in the text channel and looks like a natural response
    if not message.author.bot:
        return  # Only process bot messages (Hermes agent responses)
    if message.author.id != bot.user.id:
        return
    ch_id = state.get("text_channel_id")
    if not ch_id or message.channel.id != int(ch_id):
        return

    content = message.content.strip()
    if not content:
        return
    # Skip tool calls, commands, status messages
    # Skip Discord mentions/embeds

    # Strip emoji for clean TTS output — keep CJK + ASCII + common punctuation only
    import re as _re
    # Strip emoji + filter to readable chars for TTS
    # Keep: CJK, CJK punctuation, fullwidth, basic ASCII, common punctuation
    clean = ''.join(c for c in content if (
        '\u4e00' <= c <= '\u9fff' or    # CJK
        '\u3000' <= c <= '\u303f' or    # CJK punctuation
        '\uff00' <= c <= '\uffef' or    # fullwidth
        '\u0020' <= c <= '\u007e' or    # basic ASCII
        c in '，。！？、；：""''（）【】《》—…·'
    ))
    clean = _re.sub(r'\s+', ' ', clean).strip()
    if not clean or len(clean) < 3:
        return

    # Skip if content looks like code blocks, tool output, or system messages
    skip_patterns = (
        '```', '/home/', '/tmp/', 'File "', 'Traceback',
        'line ', 'Error:', 'exit code', 'stderr', 'stdout',
        '❌', '✅', '⚠️', '📝', '🔊', '🔇', '📤', '💾',
        '!set', '!join', '!leave', '!listen', '!stoplisten',
        '!vc_', '!test_capture', '!set_text_channel', '!debug',
    )
    if any(p in content for p in skip_patterns):
        return
    if clean.startswith('/') or clean.startswith('./') or clean.startswith('--'):
        return
    # Skip if mostly non-conversational (ratio of symbols too high)
    symbol_ratio = sum(1 for c in clean if c in '{}[]=<>|\\/`~!@#$%^&*+') / max(len(clean), 1)
    if symbol_ratio > 0.2:
        return

    # Preprocess for TTS: strip markdown, convert symbols to spoken words
    tts_text = _preprocess_tts(clean)
    if not tts_text or len(tts_text) < 3:
        return

    log(f"🗣️ Auto-TTS: {tts_text[:60]}...")
    ts = int(time.time() * 1000)
    mp3_path = f"/tmp/hermes_tts_{ts}.mp3"
    wav_path = QUEUE_DIR / f"hermes_{ts}.wav"

    try:
        subprocess.run(
            ["edge-tts", "--voice", "zh-HK-WanLungNeural",
             "--text", tts_text, "--write-media", mp3_path],
            capture_output=True, timeout=30,
        )
        if Path(mp3_path).exists():
            subprocess.run(
                ["ffmpeg", "-y", "-i", mp3_path,
                 "-ar", "48000", "-ac", "1", "-sample_fmt", "s16",
                 str(wav_path)],
                capture_output=True, timeout=30,
            )
            Path(mp3_path).unlink(missing_ok=True)
            log(f"🔊 TTS queued: hermes_{ts}.wav")
    except Exception as e:
        log(f"❌ Auto-TTS error: {e}")

# ── TTS Preprocessing ────────────────────────────────────────

def _preprocess_tts(text: str) -> str:
    """Preprocess text for TTS: strip markdown, convert symbols to spoken words."""
    import re
    t = text

    # 1. Strip markdown formatting (remove markers, keep inner text)
    t = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', t)
    t = re.sub(r'_{2,3}(.+?)_{2,3}', r'\1', t)
    t = re.sub(r'~~(.+?)~~', r'\1', t)
    t = re.sub(r'`([^`]+)`', r'\1', t)
    t = re.sub(r'^#{1,6}\s+', '', t, flags=re.MULTILINE)
    t = re.sub(r'^>\s?', '', t, flags=re.MULTILINE)

    # 2. Convert symbols to spoken equivalents (Cantonese-friendly)
    # Order matters: multi-char before single-char
    replacements = [
        # Arrows / comparisons
        ('==', '等於'),
        ('!=', '不等於'),
        ('>=', '大過或者等於'),
        ('<=', '細過或者等於'),
        ('->', '去'),
        ('=>', '變成'),
        # Slash and ampersand
        (' / ', ' 或者 '),
        ('/', ' 或者 '),
        (' & ', ' 同 '),
        # Single symbols
        ('+', '加'),
        ('=', '等於'),
        ('%', '百分比'),
        ('$', '美金 '),
        ('~', '大概'),
        ('@', ' at '),
        ('|', ' 或者 '),
        ('^', '次方'),
        ('<', '細過'),
        ('>', '大過'),
    ]

    for old, new in replacements:
        t = t.replace(old, new)

    # 3. #号处理: "#1" → "一號" is complex, edge-tts can handle "號1" reasonably
    # Just remove bare # that wasn't in a number context
    t = t.replace('#', '號')

    # 4. Clean up whitespace
    t = re.sub(r'\s+', ' ', t).strip()
    return t


# ── State persistence ─────────────────────────────────────────
STATE_FILE = HERMES_HOME / "voice_bot_state.json"

def _load_state():
    """Load persisted state from disk."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if "text_channel_id" in data:
                state["text_channel_id"] = data["text_channel_id"]
                log(f"📝 Restored text_channel_id: {state['text_channel_id']}")
        except Exception as e:
            log(f"⚠️ Could not load state: {e}")

def _save_state():
    """Persist current state to disk."""
    try:
        data = {}
        if "text_channel_id" in state and state["text_channel_id"]:
            data["text_channel_id"] = state["text_channel_id"]
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        log(f"⚠️ Could not save state: {e}")


@bot.event
async def on_ready():
    log(f"✅ Logged in as {bot.user}")
    log(f"   Guilds: {[g.name for g in bot.guilds]}")

    guild_id = os.environ.get("VOICE_GUILD_ID")
    channel_name = os.environ.get("VOICE_CHANNEL_NAME", "Hermes語音")
    text_channel_id = os.environ.get("TRANSCRIPT_CHANNEL_ID")

    if text_channel_id:
        state["text_channel_id"] = int(text_channel_id)
        log(f"📝 Transcription channel: {text_channel_id}")
        _save_state()
    else:
        # Try loading from persisted state
        _load_state()

    if guild_id:
        guild = bot.get_guild(int(guild_id))
        if guild:
            await join_or_create(guild, channel_name)

    # Start playback infrastructure
    asyncio.create_task(unix_socket_server())
    asyncio.create_task(watch_queue_fast())

    # Start ASR worker thread
    loop = asyncio.get_running_loop()
    threading.Thread(target=run_asr_worker, args=(loop, bot), daemon=True).start()

    # Listening auto-started by join_or_create

def start_listening():
    """Start listening on the voice connection using VAD-based sink."""
    if not state["vc"] or not state["vc"].is_connected():
        log("⚠️ Can't start listening: not connected")
        return
    if state["listening"]:
        log("⚠️ Already listening")
        return

    sink = VADUtteranceSink()
    state["vc"].listen(sink)
    state["listening"] = True
    log("🎧 Listening started (VAD endpoint detection)")

async def _restart_listening():
    """Called from auto-restart thread to re-enable listening."""
    if not state["vc"] or not state["vc"].is_connected():
        log("⚠️ Auto-restart: not connected")
        state["listening"] = False
        return
    try:
        state["listening"] = False
        sink = VADUtteranceSink()
        state["vc"].listen(sink)
        state["listening"] = True
        log("🔄 Listening auto-restarted")
    except Exception as e:
        log(f"❌ Auto-restart listen failed: {e}")
        state["listening"] = False

def stop_listening():
    """Stop listening."""
    if state["vc"] and state["listening"]:
        try:
            state["vc"].stop_listening()
        except Exception as e:
            log(f"⚠️ Stop listening error: {e}")
        state["listening"] = False
        log("🎧 Listening stopped")

async def join_or_create(guild, name):
    chan = discord.utils.get(guild.voice_channels, name=name)
    if not chan:
        log(f"❌ Channel #{name} doesn't exist")
        return
    try:
        if state["vc"] and state["vc"].is_connected():
            # Reconnect with VoiceRecvClient if needed
            if not isinstance(state["vc"], voice_recv.VoiceRecvClient):
                await state["vc"].disconnect()
                state["vc"] = None

        if not state["vc"]:
            state["vc"] = await chan.connect(
                cls=voice_recv.VoiceRecvClient,
                self_deaf=False,
                self_mute=False,
            )
        else:
            await state["vc"].move_to(chan)

        state["channel_id"] = chan.id
        log(f"🔊 Joined #{chan.name} (VoiceRecvClient)")

        # Auto-start listening
        start_listening()
    except Exception as e:
        log(f"❌ Join failed: {e}")

# ── Commands ─────────────────────────────────────────────────

@bot.command(name="join")
async def cmd_join(ctx, *, name="Hermes語音"):
    await join_or_create(ctx.guild, name)
    await ctx.send(f"🔊 Joined **#{name}**")

@bot.command(name="leave")
async def cmd_leave(ctx):
    stop_listening()
    if state["vc"] and state["vc"].is_connected():
        await state["vc"].disconnect()
        state["vc"] = None
        await ctx.send("👋 Left")
    else:
        await ctx.send("❌ Not connected")

@bot.command(name="listen")
async def cmd_listen(ctx):
    """Start listening to voice channel."""
    if not state["vc"] or not state["vc"].is_connected():
        await ctx.send("❌ Not connected to voice channel. Use `!join` first.")
        return
    start_listening()
    await ctx.send("🎧 **Listening started** — VAD endpoint detection active")

@bot.command(name="stoplisten")
async def cmd_stoplisten(ctx):
    """Stop listening."""
    stop_listening()
    await ctx.send("🎧 **Listening stopped**")

@bot.command(name="vc_on")
async def cmd_vc_on(ctx):
    await set_enabled(True)
    await ctx.send("🔊 Voice playback **ON**")

@bot.command(name="vc_off")
async def cmd_vc_off(ctx):
    await set_enabled(False)
    await ctx.send("🔇 Voice playback **OFF**")

@bot.command(name="vc_toggle")
async def cmd_vc_toggle(ctx):
    await set_enabled(not state["enabled"])
    await ctx.send(f"{'🔊 ON' if state['enabled'] else '🔇 OFF'}")

@bot.command(name="vc_debug")
async def cmd_debug(ctx):
    lines = []
    if state["vc"] and state["vc"].is_connected():
        ch = state["vc"].channel
        lines.append(f"🔊 **#{ch.name}**")
        lines.append(f"   Playing: {state['vc'].is_playing()}")
        lines.append(f"   Listening: {state['listening']}")
        lines.append(f"   Client type: {type(state['vc']).__name__}")
        bot_member = ctx.guild.get_member(bot.user.id)
        if bot_member:
            perms = ch.permissions_for(bot_member)
            lines.append(f"   Speak: {perms.speak}")
    else:
        lines.append("❌ Not connected")
    await ctx.send("\n".join(lines))

@bot.command(name="set_text_channel")
async def cmd_set_text(ctx):
    """Set the current channel as the transcription target."""
    state["text_channel_id"] = ctx.channel.id
    _save_state()
    await ctx.send(f"📝 Transcriptions will appear here (<#{ctx.channel.id}>)")


class RawCaptureSink(voice_recv.AudioSink):
    """Captures raw PCM for a fixed duration, no VAD."""
    def __init__(self, duration_ms=3000):
        super().__init__()
        self.duration_ms = duration_ms
        self.buffers: dict[int, list[bytes]] = {}
        self._started = time.time()

    def wants_opus(self):
        return False

    def write(self, user, data):
        if time.time() - self._started > self.duration_ms / 1000:
            return
        uid = user.id if user else 0
        if uid not in self.buffers:
            self.buffers[uid] = []
        pcm = data.pcm
        if pcm and len(pcm) >= 4:
            import array
            samples = array.array('h', pcm)
            n_pairs = len(samples) // 2
            mono = array.array('h', [0]) * n_pairs
            for i in range(n_pairs):
                mono[i] = samples[i * 2]
            self.buffers[uid].append(mono.tobytes())

    def cleanup(self):
        pass

    def is_done(self):
        return time.time() - self._started > self.duration_ms / 1000


@bot.command(name="test_capture")
async def cmd_test_capture(ctx, duration: int = 3):
    """Capture raw audio for N seconds (bypass VAD) to test DAVE decrypt."""
    if not state["vc"] or not state["vc"].is_connected():
        await ctx.send("❌ Not connected")
        return

    stop_listening()
    await ctx.send(f"🎤 Capturing {duration}s raw audio (no VAD)...")

    sink = RawCaptureSink(duration_ms=duration * 1000)
    state["vc"].listen(sink)

    while not sink.is_done():
        await asyncio.sleep(0.5)

    state["vc"].stop_listening()

    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    saved = []
    for uid, frames in sink.buffers.items():
        if not frames:
            continue
        pcm_data = b"".join(frames)
        wav_path = SEGMENT_DIR / f"rawtest_{uid}_{ts}.wav"
        import wave as _w
        with _w.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(pcm_data)

        dur_s = len(pcm_data) / 96000
        non_zero = sum(1 for b in pcm_data if b != 0) * 100 / len(pcm_data) if pcm_data else 0
        saved.append(f"{wav_path.name} ({dur_s:.1f}s, {non_zero:.0f}% non-zero)")
        log(f"📹 Raw test saved: {wav_path.name}")

    # Restart normal listening
    start_listening()

    if saved:
        await ctx.send(f"✅ Captured:\n" + "\n".join(saved))
    else:
        await ctx.send("⚠️ No audio captured (no packets received)")

# ── Main ─────────────────────────────────────────────────────

async def main():
    args = sys.argv[1:]
    os.environ.setdefault("VOICE_CHANNEL_NAME", "Hermes語音")
    for i, arg in enumerate(args):
        if arg == "--guild" and i + 1 < len(args):
            os.environ["VOICE_GUILD_ID"] = args[i + 1]
        elif arg == "--channel" and i + 1 < len(args):
            os.environ["VOICE_CHANNEL_NAME"] = args[i + 1]
        elif arg == "--text-channel" and i + 1 < len(args):
            os.environ["TRANSCRIPT_CHANNEL_ID"] = args[i + 1]

    if not TOKEN:
        log("❌ No token")
        sys.exit(1)

    await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("\n👋 Shutdown")
