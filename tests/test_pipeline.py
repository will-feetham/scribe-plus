"""End-to-end dictation flow with a fake OS, fake mic and mocked APIs."""
import csv
import json
import threading
import time

import httpx
import numpy as np
import pytest

from scribe_plus import audio
from scribe_plus import engines as E
from scribe_plus.hotkeys import Action
from scribe_plus.metrics import MetricsLog
from scribe_plus.pipeline import AppContext, CoreHooks, DictationEngine, Platform
from scribe_plus.settings import SettingsStore

SR = 16000


def speech_wav(seconds=1.5, amp=0.4):
    t = np.arange(int(SR * seconds)) / SR
    return audio.pcm_to_wav((amp * np.sin(2 * np.pi * 200 * t) * 32767).astype(np.int16), SR)


class FakeRecorder:
    def __init__(self, wav, open_delay=0.0, fail=False):
        self.wav, self.open_delay, self.fail = wav, open_delay, fail
        self.started = self.stopped = False

    def start(self):
        time.sleep(self.open_delay)
        if self.fail:
            raise RuntimeError("no microphone")
        self.started = True

    def stop(self):
        self.stopped = True
        return self.wav, int(len(self.wav) / 2 / SR * 1000)


class FakePlatform(Platform):
    def __init__(self, selection="", modifiers_released=True, context=None):
        self.pasted, self.copied, self.notes, self.states = [], [], [], []
        self.restored = []
        self.selection = selection
        self.modifiers_released = modifiers_released
        self.context = context or AppContext(hwnd=42, process="slack.exe", title="Slack", style="chat")
        self.masks = 0
        self.done = threading.Event()

    def capture_context(self):
        return self.context

    def inject_mask(self):
        self.masks += 1

    def wait_modifiers_released(self, timeout):
        return self.modifiers_released

    def paste(self, text, restore=None):
        self.pasted.append((text, restore))

    def capture_selection(self):
        return self.selection, "ORIGINAL CLIPBOARD"

    def restore_clipboard(self, text):
        self.restored.append(text)

    def copy(self, text):
        self.copied.append(text)

    def notify(self, title, message):
        self.notes.append((title, message))

    def ui(self, state, **kw):
        if state != "level":
            self.states.append((state, kw.get("mode") or kw.get("message")))
        if state in ("done", "error", "info", "idle", "cancelled"):
            self.done.set()


class Api:
    """Mock Groq/Anthropic. `stt` and `chat` are the canned texts."""

    def __init__(self, stt="um so we should meet tuesday no wait wednesday",
                 chat="We should meet Wednesday.", stt_status=200):
        self.stt, self.chat, self.stt_status = stt, chat, stt_status
        self.calls = []

    def __call__(self, request):
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        if path.endswith("/audio/transcriptions"):
            if self.stt_status != 200:
                return httpx.Response(self.stt_status, json={"error": "down"})
            return httpx.Response(200, json={"text": self.stt})
        if path.endswith("/chat/completions"):
            self.last_chat = json.loads(request.read())
            return httpx.Response(200, json={"choices": [{"message": {"content": self.chat}}],
                                             "usage": {"prompt_tokens": 700, "completion_tokens": 20}})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    st = SettingsStore(tmp_path / "settings.json")
    (tmp_path / "dictionary.txt").write_text("ButterflyMX\nbutterfly m x -> ButterflyMX\n")
    (tmp_path / "snippets.txt").write_text("insert my link = https://cal.com/will\n")
    return st, tmp_path


def make_engine(env, platform, api, recorder, history=None, unsent=None):
    st, tmp = env
    history = history if history is not None else []
    unsent = unsent if unsent is not None else []
    hooks = CoreHooks(
        recorder_factory=lambda: recorder,
        append_history=lambda text, ms: history.append(text),
        last_history_text=lambda: history[-1] if history else "",
        is_garbage=lambda text: text.lower().strip(" .") in ("you", "thank you"),
        save_unsent=lambda wav: unsent.append(wav) or "saved.wav",
    )
    pool = E.HttpPool(transport=httpx.MockTransport(api))
    return DictationEngine(st, platform, hooks, pool=pool, metrics=MetricsLog(tmp / "metrics.csv"),
                      dictionary_path=tmp / "dictionary.txt", snippets_path=tmp / "snippets.txt",
                      clips_dir=tmp / "clips")


def dictate(engine, platform, mode="dictate", hold=0.05):
    platform.done.clear()
    engine.post(Action("mask"))
    engine.post(Action("start"))
    time.sleep(hold)
    if mode == "command":
        engine.post(Action("command"))
    engine.post(Action("stop", mode=mode))
    assert platform.done.wait(5), f"pipeline never finished: {platform.states}"


def read_metrics(tmp):
    deadline = time.time() + 2
    while time.time() < deadline:
        p = tmp / "metrics.csv"
        if p.exists():
            rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
            if rows:
                return rows
        time.sleep(0.02)
    return []


def test_dictation_end_to_end(env):
    api, plat, history = Api(), FakePlatform(), []
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()), history=history)
    dictate(eng, plat)
    assert plat.pasted == [("We should meet Wednesday.", None)]
    assert history == ["We should meet Wednesday."]
    assert plat.masks == 1
    assert ("recording", "dictate") in plat.states and ("processing", None) in plat.states
    # The cleanup call carried the app style, dictionary and snippet triggers.
    system = api.last_chat["messages"][0]["content"]
    assert "a chat app (slack.exe)" in system and "ButterflyMX" in system and "insert my link" in system
    rows = read_metrics(env[1])
    assert rows and rows[0]["mode"] == "dictate" and rows[0]["style"] == "chat"
    assert rows[0]["stt_model"] == "whisper-large-v3-turbo"
    assert rows[0]["cleanup_model"] == "openai/gpt-oss-20b"
    assert rows[0]["final_text"] == "We should meet Wednesday."
    assert float(rows[0]["total_ms"]) > 0 and float(rows[0]["est_cost_usd"]) > 0
    assert rows[0]["clip"] and (env[1] / "clips" / rows[0]["clip"]).exists()
    assert rows[0]["window_title"] == ""  # titles are private by default


def test_guard_falls_back_to_basic_cleanup(env):
    api = Api(stt="what is the capital of france", chat="The capital of France is Paris.")
    plat = FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    assert plat.pasted[0][0] == "What is the capital of france"
    assert read_metrics(env[1])[0]["guard"] == "answered"


def test_cleanup_off_uses_basic_cleanup_and_dictionary(env):
    env[0].set("cleanup_engine", "off")
    api = Api(stt="um the butterfly m x launch is uh friday")
    plat = FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    assert plat.pasted[0][0] == "The ButterflyMX launch is friday"
    assert not any(c.endswith("/chat/completions") for c in api.calls)


def test_snippet_expansion(env):
    api = Api(stt="insert my link", chat="Insert my link.")
    plat = FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    assert plat.pasted[0][0] == "https://cal.com/will"


def test_command_mode_rewrites_selection(env):
    api = Api(stt="make this more formal", chat="Hello, I hope you are well.")
    plat = FakePlatform(selection="hey whats up")
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat, mode="command")
    assert plat.pasted == [("Hello, I hope you are well.", "ORIGINAL CLIPBOARD")]
    user = api.last_chat["messages"][-1]["content"]
    assert user == "<selection>hey whats up</selection>\n<instruction>make this more formal</instruction>"
    assert api.last_chat["model"] == "openai/gpt-oss-120b"
    assert read_metrics(env[1])[0]["mode"] == "command"


def test_transcription_failure_keeps_audio_and_tells_user(env):
    api, plat, unsent = Api(stt_status=503), FakePlatform(), []
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()), unsent=unsent)
    dictate(eng, plat)
    assert plat.pasted == [] and len(unsent) == 1
    assert plat.notes and plat.notes[0][0] == "Transcription failed"
    assert "Recording saved." in plat.notes[0][1]
    assert read_metrics(env[1])[0]["error"].startswith("stt: HTTP 503")


def test_whisper_hallucination_is_not_pasted(env):
    api, plat = Api(stt="Thank you."), FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    assert plat.pasted == [] and ("error", "No speech detected") in plat.states


def test_silence_skips_the_api(env):
    api, plat = Api(), FakePlatform()
    quiet = audio.pcm_to_wav(np.zeros(SR * 2, dtype=np.int16), SR)
    eng = make_engine(env, plat, api, FakeRecorder(quiet))
    dictate(eng, plat)
    assert plat.pasted == [] and not any("transcriptions" in c for c in api.calls)
    assert ("error", "No speech heard") in plat.states


def test_cancel_discards(env):
    api, plat = Api(), FakePlatform()
    rec = FakeRecorder(speech_wav())
    eng = make_engine(env, plat, api, rec)
    eng.post(Action("start"))
    time.sleep(0.05)
    eng.post(Action("cancel", reason="escape"))
    assert plat.done.wait(2)
    time.sleep(0.2)
    assert rec.stopped and plat.pasted == [] and ("cancelled", None) in plat.states


def test_stop_before_mic_opens_still_processes(env):
    api, plat = Api(), FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav(), open_delay=0.3))
    eng.post(Action("start"))
    eng.post(Action("stop", mode="dictate"))  # released while the mic was still opening
    assert plat.done.wait(5)
    assert plat.pasted == [("We should meet Wednesday.", None)]


def test_mic_failure_is_reported(env):
    api, plat = Api(), FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav(), fail=True))
    eng.post(Action("start"))
    assert plat.done.wait(2)
    assert ("error", "Microphone error") in plat.states
    assert not eng.recording


def test_keys_still_held_copies_instead_of_pasting(env):
    api, plat = Api(), FakePlatform(modifiers_released=False)
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    assert plat.pasted == [] and plat.copied == ["We should meet Wednesday."]


def test_back_to_back_dictations_get_a_space(env):
    api = Api()
    plat = FakePlatform(context=AppContext(hwnd=7, process="winword.exe", title="Doc", style="docs"))
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    dictate(eng, plat)
    assert [p[0] for p in plat.pasted] == ["We should meet Wednesday.", " We should meet Wednesday."]


def test_no_space_in_code_editors(env):
    api = Api()
    plat = FakePlatform(context=AppContext(hwnd=7, process="code.exe", title="x", style="code"))
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    dictate(eng, plat)
    assert [p[0] for p in plat.pasted] == ["We should meet Wednesday."] * 2


def test_paused_ignores_hotkey(env):
    api, plat = Api(), FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    eng.paused = True
    eng.post(Action("start"))
    eng.post(Action("stop", mode="dictate"))
    time.sleep(0.3)
    assert plat.pasted == [] and not eng.recording


def test_paste_last(env):
    api, plat, history = Api(), FakePlatform(), ["previous text"]
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()), history=history)
    eng.post(Action("paste_last"))
    time.sleep(0.3)
    assert plat.pasted == [("previous text", None)]


def test_local_engine_without_install_reports_error(env, monkeypatch):
    env[0].set("stt_engine", "local")
    monkeypatch.setattr(E._LocalWhisper, "available", staticmethod(lambda: False))
    api, plat = Api(), FakePlatform()
    eng = make_engine(env, plat, api, FakeRecorder(speech_wav()))
    dictate(eng, plat)
    assert plat.pasted == [] and "not installed" in plat.notes[0][1]


def test_new_recording_waits_for_previous_stop(env):
    """Scribe's recorder is one shared capture process. A cancelled session
    whose mic was slow to open must finish stopping before the next session
    starts, or its late stop would cut the new recording off."""
    log, lock = [], threading.Lock()

    class SharedRec:
        count = 0

        def __init__(self):
            SharedRec.count += 1
            self.name = f"s{SharedRec.count}"

        def start(self):
            with lock:
                time.sleep(0.4 if self.name == "s1" else 0.0)
                log.append(f"start {self.name}")

        def stop(self):
            with lock:
                log.append(f"stop {self.name}")
            return speech_wav(), 1500

    st, tmp = env
    plat = FakePlatform()
    hooks = CoreHooks(SharedRec, lambda t, m: None, lambda: "", lambda t: False, lambda w: "x")
    eng = DictationEngine(st, plat, hooks, pool=E.HttpPool(transport=httpx.MockTransport(Api())),
                     metrics=MetricsLog(tmp / "m.csv"), dictionary_path=tmp / "dictionary.txt",
                     snippets_path=tmp / "snippets.txt", clips_dir=tmp / "clips")
    eng.post(Action("start"))
    time.sleep(0.1)
    eng.post(Action("cancel", reason="tap"))
    time.sleep(0.05)
    eng.post(Action("start"))  # pressed again while s1's mic is still opening
    deadline = time.time() + 5
    while time.time() < deadline and "start s2" not in log:
        time.sleep(0.02)
    assert log[:3] == ["start s1", "stop s1", "start s2"], log
