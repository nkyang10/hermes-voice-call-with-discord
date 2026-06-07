"""Tests for discord_voice_bot_v4.py (offline, no Discord/ComfyUI required)."""

import os
import sys
import importlib.util
from pathlib import Path

# ── Test the functions that can be imported ──

SCRIPT = Path(__file__).parent.parent / "discord_voice_bot_v4.py"

def _import_module():
    """Import the script as a module (sidesteps Discord token check)."""
    # Set dummy env vars to allow import
    os.environ.setdefault("DISCORD_BOT_TOKEN", "test_token_123")
    os.environ.setdefault("HERMES_HOME", "/tmp/hermes_test")
    os.environ.setdefault("COMFYUI_URL", "http://test:8000")

    spec = importlib.util.spec_from_file_location("voice_bot", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── _preprocess_tts tests ──

def test_tts_strips_markdown_bold():
    mod = _import_module()
    assert mod._preprocess_tts("Hello **world**") == "Hello world"

def test_tts_strips_inline_code():
    mod = _import_module()
    assert mod._preprocess_tts("Use `pip install`") == "Use pip install"

def test_tts_converts_symbols():
    mod = _import_module()
    result = mod._preprocess_tts("a == b")
    assert "等於" in result

def test_tts_converts_slash():
    mod = _import_module()
    result = mod._preprocess_tts("A / B")
    assert "或者" in result

def test_tts_handles_hash():
    mod = _import_module()
    result = mod._preprocess_tts("#1")
    assert "號" in result  # # → 號

def test_tts_removes_heading():
    mod = _import_module()
    assert mod._preprocess_tts("## Title") == "Title"

def test_tts_handles_empty():
    mod = _import_module()
    assert mod._preprocess_tts("") == ""

def test_tts_converts_percentage():
    mod = _import_module()
    result = mod._preprocess_tts("50%")
    assert "百分比" in result

def test_tts_converts_greater_than():
    mod = _import_module()
    result = mod._preprocess_tts("x > 5")
    assert "大過" in result

def test_tts_cleanup_whitespace():
    mod = _import_module()
    result = mod._preprocess_tts("  spaced   out  ")
    assert result == "spaced out"


# ── is_natural_speech tests ──

def test_natural_cjk_speech():
    mod = _import_module()
    assert mod.is_natural_speech("你好今日天氣點樣") is True

def test_natural_english_speech():
    mod = _import_module()
    assert mod.is_natural_speech("hello how are you") is True

def test_mixed_cjk_ascii():
    mod = _import_module()
    assert mod.is_natural_speech("今日天氣很好 but raining") is True

def test_code_like_symbols_rejected():
    mod = _import_module()
    assert mod.is_natural_speech("a == b && c < d || e > f") is False

def test_file_path_rejected():
    mod = _import_module()
    assert mod.is_natural_speech("/home/user/file.py") is False

def test_url_rejected():
    mod = _import_module()
    assert mod.is_natural_speech("http://example.com/test") is False

def test_too_short_rejected():
    mod = _import_module()
    assert mod.is_natural_speech("ab") is False

def test_python_extension_rejected():
    mod = _import_module()
    assert mod.is_natural_speech("check the .py file") is False

def test_mostly_symbols_rejected():
    mod = _import_module()
    # "a == b && c" has 4 symbols out of 9 chars = 44%
    assert mod.is_natural_speech("a == b && c") is False


# ── _execute_voice_command tests ──

def test_stop_command():
    mod = _import_module()
    assert mod._execute_voice_command("stop") == "stop"

def test_cantonese_stop_閉嘴():
    mod = _import_module()
    assert mod._execute_voice_command("閉嘴") == "stop"

def test_cantonese_stop_收聲():
    mod = _import_module()
    assert mod._execute_voice_command("收聲") == "stop"

def test_cantonese_stop_收皮():
    mod = _import_module()
    assert mod._execute_voice_command("收皮") == "stop"

def test_non_command_returns_none():
    mod = _import_module()
    assert mod._execute_voice_command("hello") is None

def test_case_insensitive():
    mod = _import_module()
    assert mod._execute_voice_command("STOP") == "stop"


# ── Module import ──

def test_module_imports():
    """Verify the module can be imported without crashing (token skips Discord)."""
    mod = _import_module()
    assert hasattr(mod, "VADUtteranceSink")
    assert hasattr(mod, "_preprocess_tts")
    assert hasattr(mod, "is_natural_speech")
    assert hasattr(mod, "_execute_voice_command")
    assert hasattr(mod, "COMFYUI_URL")
    assert hasattr(mod, "VADUtteranceSink")
    # MIN_UTTERANCE_DURATION_MS and MIN_UTTERANCE_RMS are now
    # hardcoded in VADUtteranceSink.__init__, not module-level constants
