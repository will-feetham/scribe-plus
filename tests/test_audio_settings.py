import json

import numpy as np

from scribe_plus import audio
from scribe_plus import settings as S

SR = 16000


def tone(seconds, amp, freq=220.0):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def silence(seconds, amp=0.0005):
    rng = np.random.default_rng(1)
    return (rng.normal(0, amp, int(SR * seconds)) * 32767).astype(np.int16)


def test_silence_is_detected():
    p = audio.prepare(audio.pcm_to_wav(silence(2.0), SR))
    assert p.silent


def test_edges_are_trimmed_with_margin():
    pcm = np.concatenate([silence(1.5), tone(1.0, 0.5), silence(1.5)])
    p = audio.prepare(audio.pcm_to_wav(pcm, SR), boost=False)
    assert not p.silent
    assert abs(p.duration_s - 4.0) < 0.01
    assert 1.0 <= p.kept_s <= 1.5  # speech plus ~200 ms on each side
    pcm_out, _ = audio.wav_to_pcm(p.wav)
    assert len(pcm_out) / SR == p.kept_s


def test_quiet_voice_is_boosted():
    pcm = tone(1.0, 0.1)  # peak about -20 dBFS: quiet speech
    p = audio.prepare(audio.pcm_to_wav(pcm, SR), trim=False)
    assert 16 < p.gain_db < 18
    out, _ = audio.wav_to_pcm(p.wav)
    peak_db = audio.dbfs(np.max(np.abs(out)) / 32768)
    assert -3.5 < peak_db < -2.5


def test_whisper_boost_is_capped():
    pcm = tone(1.0, 0.02)  # about -34 dBFS: a whisper
    p = audio.prepare(audio.pcm_to_wav(pcm, SR), trim=False)
    assert p.gain_db == audio.MAX_BOOST_DB
    out, _ = audio.wav_to_pcm(p.wav)
    assert abs(audio.dbfs(np.max(np.abs(out)) / 32768) - (-34 + 24)) < 0.5


def test_loud_voice_untouched():
    wav = audio.pcm_to_wav(tone(1.0, 0.8), SR)
    p = audio.prepare(wav, trim=False)
    assert p.gain_db == 0 and p.wav == wav


def test_level_meter_range():
    assert audio.level_from_pcm_bytes(b"") == 0.0
    assert audio.level_from_pcm_bytes(tone(0.1, 0.9).tobytes()) > 0.9
    assert audio.level_from_pcm_bytes(silence(0.1, 0.00001).tobytes()) == 0.0


def test_settings_written_merged_and_reloaded(tmp_path):
    path = tmp_path / "settings.json"
    st = S.SettingsStore(path)
    on_disk = json.loads(path.read_text())
    assert on_disk["hotkey"] == "ctrl_win" and on_disk["cleanup_models"]["groq"] == "openai/gpt-oss-20b"

    path.write_text(json.dumps({"hotkey": "right_alt", "cleanup_models": {"groq": "openai/gpt-oss-120b"}}))
    import os
    import time
    os.utime(path, (time.time() + 5, time.time() + 5))
    st.maybe_reload()
    assert st.get("hotkey") == "right_alt"
    assert st.cleanup_model("groq") == "openai/gpt-oss-120b"
    assert st.cleanup_model("anthropic") == "claude-haiku-5-5"  # nested default kept
    assert json.loads(path.read_text())["stt_engine"] == "groq"  # missing keys filled in


def test_broken_settings_file_is_left_alone(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json")
    st = S.SettingsStore(path)
    assert st.get("hotkey") == "ctrl_win"
    assert path.read_text() == "{ not json"


def test_set_nested(tmp_path):
    st = S.SettingsStore(tmp_path / "s.json")
    st.set_nested("stt_models", "groq", "whisper-large-v3")
    assert st.stt_model("groq") == "whisper-large-v3"
    assert S.SettingsStore(tmp_path / "s.json").stt_model() == "whisper-large-v3"


def test_resolve_style():
    rules = S.APP_RULES
    assert S.resolve_style("slack.exe", "anything", rules) == "chat"
    assert S.resolve_style("chrome.exe", "Inbox (3) - will@example.com - Gmail - Google Chrome", rules) == "email"
    assert S.resolve_style("chrome.exe", "Claude", rules) == "ai"
    assert S.resolve_style("Code.exe", "main.py - Visual Studio Code", rules) == "code"
    assert S.resolve_style("chrome.exe", "Untitled document - Google Docs", rules) == "docs"
    assert S.resolve_style("explorer.exe", "Downloads", rules) == "default"


def test_env_key_roundtrip(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=old\nOTHER=1\n")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    S.save_env_key("GROQ_API_KEY", "  new-key ", env)
    S.save_env_key("ANTHROPIC_API_KEY", "a", env)
    assert env.read_text() == "GROQ_API_KEY=new-key\nOTHER=1\nANTHROPIC_API_KEY=a\n"
    import os
    assert os.environ["GROQ_API_KEY"] == "new-key"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_user_files_created():
    S.ensure_user_files()
    assert "heard -> written" in S.DICTIONARY_FILE.read_text()
    assert "trigger phrase = text to insert" in S.SNIPPETS_FILE.read_text()
