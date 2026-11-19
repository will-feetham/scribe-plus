"""Call transcripts: resampling, utterance cutting, the transcript and its
files, the recorder and call detection. No audio hardware involved."""
import threading
import time
from datetime import datetime

import numpy as np

from scribe_plus.calls import stream
from scribe_plus.calls.detector import DEFAULT_APPS, CallDetector, app_root, matches
from scribe_plus.calls.recorder import CallRecorder, Channel, is_noise_text, normalize
from scribe_plus.calls.segmenter import RATE, Segmenter
from scribe_plus.calls.transcript import Line, Transcript, TranscriptFiles, clock

rng = np.random.default_rng(7)


def tone(freq, seconds, rate, amp=0.5):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def speech(seconds, amp=0.1):
    """Stands in for speech: noise well above the room."""
    return (amp * rng.standard_normal(int(seconds * RATE))).astype(np.float32)


def quiet(seconds, amp=0.0005):
    return (amp * rng.standard_normal(int(seconds * RATE))).astype(np.float32)


# -- packets and resampling ------------------------------------------------------


def test_packets_survive_any_chunking():
    a, b = tone(440, 0.1, 48000), tone(880, 0.05, 16000)
    data = stream.pack(a, 48000) + stream.pack(b, 16000)
    reader, got = stream.PacketReader(), []
    for i in range(0, len(data), 37):          # arrives in awkward pieces
        got += reader.feed(data[i:i + 37])
    assert [r for r, _ in got] == [48000, 16000]
    assert np.allclose(got[0][1], a) and np.allclose(got[1][1], b)


def test_packet_reader_recovers_from_garbage():
    good = stream.pack(tone(440, 0.01, 16000), 16000)
    got = stream.PacketReader().feed(b"\x00\x01junk" + good)
    assert len(got) == 1 and got[0][0] == 16000


def test_resampling_keeps_speech_and_length():
    for rate in (48000, 44100, 24000, 16000, 8000):
        x = tone(1000, 1.0, rate)
        y = stream.resample(x, rate)
        assert abs(len(y) - 16000) <= 2, rate
        middle = y[2000:14000]
        assert 0.45 < np.max(np.abs(middle)) < 0.55, rate      # a 1 kHz tone passes untouched


def test_resampling_removes_what_16k_cannot_hold():
    y = stream.resample(tone(12000, 1.0, 48000), 48000)        # would fold down to 4 kHz
    assert np.max(np.abs(y[2000:14000])) < 0.02


def test_resampling_is_seamless_across_blocks():
    x = tone(700, 1.0, 44100)
    whole = stream.resample(x, 44100)
    r = stream.Resampler(44100)
    pieces = np.concatenate([r.process(x[i:i + 1000]) for i in range(0, len(x), 1000)])
    assert abs(len(pieces) - len(whole)) <= 1
    n = min(len(pieces), len(whole))
    assert np.max(np.abs(pieces[:n] - whole[:n])) < 1e-3


# -- cutting speech into utterances ------------------------------------------------


def feed_in_blocks(seg, x, block=1600):
    out = []
    for i in range(0, len(x), block):
        out += seg.feed(x[i:i + block])
    return out


def test_two_sentences_become_two_utterances():
    seg = Segmenter()
    x = np.concatenate([quiet(1.0), speech(2.0), quiet(1.5), speech(1.0), quiet(1.5)])
    utts = feed_in_blocks(seg, x)
    assert len(utts) == 2
    first, second = utts
    assert 0.6 <= first.start / RATE <= 1.0            # a little lead-in is kept
    assert 2.9 <= first.end / RATE <= 3.4              # ends soon after the speech does
    assert 1.8 <= first.speech_s <= 2.2
    assert 4.1 <= second.start / RATE <= 4.5
    assert len(first.samples) == first.end - first.start


def test_a_click_is_not_an_utterance():
    seg = Segmenter()
    x = np.concatenate([quiet(1.0), speech(0.08), quiet(2.0)])
    assert feed_in_blocks(seg, x) + seg.flush() == []


def test_someone_who_never_pauses_is_cut_into_pieces():
    seg = Segmenter(max_s=5.0)
    x = np.concatenate([quiet(0.5), speech(12.0), quiet(1.5)])
    utts = feed_in_blocks(seg, x) + seg.flush()
    assert len(utts) >= 3
    assert all(u.duration_s <= 5.2 for u in utts)
    covered = sum(u.duration_s for u in utts)
    assert 11.5 <= covered <= 13.0                      # nothing lost at the cuts
    for a, b in zip(utts, utts[1:]):
        assert a.end == b.start


def test_flush_returns_speech_in_progress():
    seg = Segmenter()
    assert feed_in_blocks(seg, np.concatenate([quiet(0.5), speech(1.0)])) == []
    utts = seg.flush()
    assert len(utts) == 1 and utts[0].speech_s > 0.8


def test_very_quiet_microphone_still_works():
    seg = Segmenter()
    x = np.concatenate([quiet(1.0, 0.0001), speech(1.5, 0.002), quiet(1.5, 0.0001)])   # about -54 dBFS
    assert len(feed_in_blocks(seg, x)) == 1


def test_digital_silence_then_speech():
    seg = Segmenter()
    x = np.concatenate([np.zeros(RATE, dtype=np.float32), speech(1.0), np.zeros(RATE, dtype=np.float32)])
    assert len(feed_in_blocks(seg, x)) == 1


# -- the transcript and its files ----------------------------------------------------


def test_transcript_orders_lines_and_joins_turns():
    tr = Transcript("Groove OmniDialer", datetime(2026, 11, 19, 11, 42), app="Groove OmniDialer")
    tr.add(Line(5.0, "Them", "Hi Will.", 6.0))
    tr.add(Line(0.5, "Me", "Hello, this is Will.", 2.0))       # arrives late, sorts first
    tr.add(Line(6.5, "Them", "Thanks for calling.", 8.0))     # same speaker, right after
    tr.add(Line(20.0, "Them", "One more thing.", 21.0))       # same speaker, after a long pause
    text = tr.render(datetime(2026, 11, 19, 11, 43, 5))
    assert text.startswith("# Call: Groove OmniDialer, 2026-11-19 11:42\n")
    assert "Status: in progress (updated 11:43:05)" in text
    body = text.split("\n\n", 2)[2].strip().splitlines()
    assert body == ["[00:00] Me: Hello, this is Will.",
                    "[00:05] Them: Hi Will. Thanks for calling.",
                    "[00:20] Them: One more thing."]
    tr.ended = datetime(2026, 11, 19, 11, 52, 3)
    assert "Status: ended 11:52:03 (10 min 3 s)" in tr.render()
    assert clock(3725) == "1:02:05"


def test_echo_of_the_other_side_is_spotted():
    tr = Transcript("Zoom", datetime(2026, 11, 19, 9, 0))
    tr.add(Line(10.0, "Them", "Can you send me the install date for the building?", 13.0))
    assert tr.is_echo(Line(10.4, "Me", "can you send me the install date for the building", 13.2))
    assert not tr.is_echo(Line(14.0, "Me", "Sure, it's scheduled for the fifth of December.", 16.0))
    assert not tr.is_echo(Line(60.0, "Me", "Can you send me the install date for the building?", 63.0))
    assert not tr.is_echo(Line(10.4, "Me", "Yes.", 10.8))


def test_files_are_written_whole_and_latest_follows(tmp_path):
    tr = Transcript("Groove: Acme / Lobby", datetime(2026, 11, 19, 11, 42))
    files = TranscriptFiles(tmp_path / "Calls", tr)
    files.write()
    assert files.path.name == "2026-11-19 1142 Groove Acme Lobby.md"
    tr.add(Line(1.0, "Me", "Hello.", 1.5))
    files.write()
    assert files.path.read_text(encoding="utf-8") == files.latest.read_text(encoding="utf-8")
    assert "[00:01] Me: Hello." in files.latest.read_text(encoding="utf-8")
    assert not list((tmp_path / "Calls").glob(".*.tmp"))
    again = TranscriptFiles(tmp_path / "Calls", Transcript("Groove: Acme / Lobby", datetime(2026, 11, 19, 11, 42)))
    assert again.path.name == "2026-11-19 1142 Groove Acme Lobby (2).md"     # never overwrites a call


# -- the recorder ------------------------------------------------------------------------


def test_noise_text_and_normalize():
    assert is_noise_text("you") and is_noise_text("[BLANK_AUDIO]") and is_noise_text("Thanks for watching!")
    assert not is_noise_text("Okay.") and not is_noise_text("Thank you.") and not is_noise_text("Bye.")
    loud = normalize(np.array([0.001, -0.002], dtype=np.float32))
    assert 0.06 < np.max(np.abs(loud)) < 0.07            # raised by the 30 dB limit, not to full scale
    assert np.max(np.abs(normalize(np.array([0.05, -0.1], dtype=np.float32)))) > 0.69


def make_recorder(tmp_path, transcribe, **kw):
    tr = Transcript("Test", datetime(2026, 11, 19, 11, 42))
    files = TranscriptFiles(tmp_path, tr)
    return CallRecorder(tr, files, transcribe, **kw), tr, files


def feed_realtime(rec, speaker, x, rate, t_start, block_s=0.1):
    """Feed with the arrival times a live stream would have."""
    block = int(block_s * rate)
    for i in range(0, len(x), block):
        piece = x[i:i + block]
        rec.feed(speaker, piece, rate, arrived=rec.t0 + t_start + (i + len(piece)) / rate)


def settle(rec, seconds=5.0):
    """Wait until the recorder has transcribed everything it was given."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if rec.pending() == 0:
            return True
        time.sleep(0.01)
    return False


def test_two_sided_call_end_to_end(tmp_path):
    heard = []

    def transcribe(samples, speaker):
        heard.append((speaker, len(samples) / RATE))
        return {"Me": "Hi, this is Will from ButterflyMX.", "Them": "Hey Will, good to hear from you."}[speaker]

    rec, tr, files = make_recorder(tmp_path, transcribe)
    me = np.concatenate([quiet(0.5), speech(2.0), quiet(4.0)])
    them48 = np.repeat(np.concatenate([quiet(3.5), speech(2.0), quiet(1.0)]), 3)    # the app plays at 48 kHz
    feed_realtime(rec, "Me", me, RATE, 0.0)
    feed_realtime(rec, "Them", them48, 48000, 0.0)
    assert rec.finish(timeout=5.0)
    lines = tr.lines()
    assert [(ln.speaker, ln.text) for ln in lines] == [
        ("Me", "Hi, this is Will from ButterflyMX."), ("Them", "Hey Will, good to hear from you.")]
    assert 0.1 <= lines[0].t <= 0.6 and 3.0 <= lines[1].t <= 3.7       # each side on the call's clock
    assert all(1.8 <= secs <= 2.8 for _s, secs in heard)
    text = files.latest.read_text(encoding="utf-8")
    assert "Status: ended" in text and "[00:00] Me: Hi, this is Will" in text and "[00:03] Them:" in text
    s = rec.summary()
    assert s["lines"] == 2 and s["me_utterances"] == 1 and s["them_utterances"] == 1


def test_speech_in_progress_at_hang_up_is_kept(tmp_path):
    rec, tr, _files = make_recorder(tmp_path, lambda samples, speaker: "Talk to you next week.")
    feed_realtime(rec, "Me", np.concatenate([quiet(0.5), speech(1.5)]), RATE, 0.0)   # cut off mid-sentence
    assert tr.lines() == []
    assert rec.finish(timeout=5.0)
    assert [ln.text for ln in tr.lines()] == ["Talk to you next week."]


def test_noise_and_echo_never_reach_the_transcript(tmp_path):
    replies = {"Them": iter(["Can you confirm the install date for the lobby panel?"]),
               "Me": iter(["can you confirm the install date for the lobby panel", "you"])}
    rec, tr, _files = make_recorder(tmp_path, lambda samples, speaker: next(replies[speaker]))
    feed_realtime(rec, "Them", np.concatenate([quiet(0.5), speech(2.0), quiet(1.5)]), RATE, 0.0)
    time.sleep(0.2)                                     # "Them" lands first, as it would live
    feed_realtime(rec, "Me", np.concatenate([quiet(0.6), speech(2.0), quiet(1.5), speech(1.0), quiet(1.5)]), RATE, 0.0)
    assert rec.finish(timeout=5.0)
    assert [(ln.speaker, ln.text) for ln in tr.lines()] == [
        ("Them", "Can you confirm the install date for the lobby panel?")]


def test_a_failing_speech_model_is_reported_once_and_does_not_stop_the_call(tmp_path):
    errors, calls = [], []

    def transcribe(samples, speaker):
        calls.append(speaker)
        if len(calls) == 1:
            raise RuntimeError("model not installed")
        return "Second sentence."

    rec, tr, files = make_recorder(tmp_path, transcribe, on_error=errors.append)
    feed_realtime(rec, "Me", np.concatenate([quiet(0.5), speech(1.0), quiet(1.5)]), RATE, 0.0)
    assert settle(rec) and len(errors) == 1
    feed_realtime(rec, "Me", np.concatenate([speech(1.0), quiet(1.5)]), RATE, 3.0)
    assert rec.finish(timeout=5.0)
    assert len(errors) == 1 and [ln.text for ln in tr.lines()] == ["Second sentence."]
    assert "could not be transcribed (model not installed)" in files.latest.read_text(encoding="utf-8")


def test_a_slow_model_catches_up_in_batches_and_a_late_finish_updates_the_file(tmp_path):
    release, started, sizes = threading.Event(), threading.Event(), []

    def transcribe(samples, speaker):
        sizes.append(len(samples) / RATE)
        started.set()
        release.wait(5.0)
        return f"part {len(sizes)}"

    rec, tr, files = make_recorder(tmp_path, transcribe)
    feed_realtime(rec, "Me", np.concatenate([quiet(0.5), speech(1.0), quiet(1.2)]), RATE, 0.0)
    assert started.wait(5.0)                            # the model is now busy with the first one
    rest = np.concatenate([np.concatenate([speech(1.0), quiet(1.2)]) for _ in range(3)])
    feed_realtime(rec, "Me", rest, RATE, 2.7)
    assert rec.finish(timeout=0.3) is False             # ... and still is at hang-up
    assert "still being transcribed" in files.latest.read_text(encoding="utf-8")
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and "still being transcribed" in files.latest.read_text(encoding="utf-8"):
        time.sleep(0.05)
    text = files.latest.read_text(encoding="utf-8")
    assert "still being transcribed" not in text and "part 2" in text
    assert len(sizes) == 2 and sizes[1] > 3.0           # the three waiting utterances went as one request


def test_a_gap_in_the_stream_keeps_the_clock_right():
    ch = Channel("Them", t0=1000.0)
    block = quiet(0.1)
    for i in range(10):                                  # 1 s of audio, on time
        ch.push(block, RATE, arrived=1000.0 + 0.1 * (i + 1))
    jobs = []
    for i in range(30):                                  # then nothing for 5 s, then speech
        piece = speech(0.1) if i < 15 else quiet(0.1)
        jobs += ch.push(piece, RATE, arrived=1006.0 + 0.1 * (i + 1))
    assert len(jobs) == 1 and 5.8 <= jobs[0].t <= 6.2


# -- noticing a call ----------------------------------------------------------------------

DIALER = {"pid": 501, "bundle": "com.electron.dialer.helper",
          "path": "/Applications/Groove OmniDialer.app/Contents/Frameworks/Groove OmniDialer Helper.app/Contents/MacOS/Groove OmniDialer Helper",
          "input": True, "output": True}


def test_app_matching():
    groove = DEFAULT_APPS[0]
    assert matches(groove, DIALER)
    assert matches(groove, {"bundle": "", "path": "/Applications/Groove OmniDialer.app/Contents/MacOS/Groove OmniDialer"})
    assert not matches(groove, {"bundle": "com.google.Chrome.helper", "path": "/Applications/Google Chrome.app/x"})
    assert app_root(DIALER["path"]) == "/Applications/Groove OmniDialer.app/"
    assert app_root("/usr/bin/afplay") == ""


def test_call_starts_after_a_moment_and_ends_after_a_moment():
    d = CallDetector(DEFAULT_APPS, start_delay=1.0, end_delay=1.5)
    d.update([DIALER], now=100.0)
    assert d.poll(100.5) is None                         # could be a blip
    event = d.poll(101.1)
    assert event and event[0] == "start" and event[1].name == "Groove OmniDialer"
    assert event[1].roots == ["/Applications/Groove OmniDialer.app/"]
    assert d.poll(130.0) is None
    d.update([dict(DIALER, input=False)], now=200.0)     # hung up; still playing a tone
    assert d.poll(200.1) is None
    assert d.poll(201.0) is None
    event = d.poll(201.7)
    assert event and event[0] == "stop"
    assert d.poll(205.0) is None and d.active is None


def test_brief_mic_use_and_brief_dropouts_are_ignored():
    d = CallDetector(DEFAULT_APPS)
    d.update([DIALER], now=10.0)
    d.update([], now=10.4)                               # let go again before a call began
    assert d.poll(12.0) is None
    d.update([DIALER], now=20.0)
    assert d.poll(21.2)[0] == "start"
    d.update([], now=30.0)                               # a device switch mid-call
    assert d.poll(30.8) is None
    d.update([DIALER], now=31.0)
    assert d.poll(33.0) is None and d.active is not None


def test_unwatched_and_disabled_apps_do_not_start_calls():
    d = CallDetector(DEFAULT_APPS)
    chrome = {"pid": 7, "bundle": "com.google.Chrome.helper", "path": "/Applications/Google Chrome.app/x", "input": True}
    python = {"pid": 8, "bundle": "", "path": "/usr/bin/python3", "input": True}
    d.update([chrome, python], now=0.0)
    assert d.poll(5.0) is None                           # Meet in Chrome is off until you turn it on
    apps = [dict(a, enabled=True) for a in DEFAULT_APPS]
    d = CallDetector(apps)
    d.update([chrome], now=0.0)
    assert d.poll(1.5)[1].name == "Google Meet (Chrome)"


# -- the manager: a whole call with a fake OS ------------------------------------------------

import httpx  # noqa: E402

from scribe_plus import engines as E  # noqa: E402
from scribe_plus.calls.manager import CallManager  # noqa: E402
from scribe_plus.settings import SettingsStore  # noqa: E402


class FakeSource:
    """Stands in for a capture helper: plays canned audio into the call."""

    def __init__(self, on_audio, audio=None, rate=RATE, fail=False):
        self.on_audio, self.audio, self.rate, self.fail = on_audio, audio, rate, fail
        self.started = self.stopped = False
        self.last_error = ""

    def start(self):
        if self.fail:
            raise RuntimeError("helper missing")
        self.started = True
        if self.audio is not None:
            now = time.monotonic()
            block = self.rate // 10
            for i in range(0, len(self.audio), block):
                piece = self.audio[i:i + block]
                self.on_audio(piece, self.rate, now + (i + len(piece)) / self.rate)

    def stop(self, timeout=1.5):
        self.stopped = True


class FakeCallPlatform:
    def __init__(self, them=None, ok=True):
        self.them, self.ok = them, ok
        self.on_procs = None
        self.taps = []

    def available(self):
        return (True, "") if self.ok else (False, "call recording needs macOS 14.2 or newer")

    def start_watch(self, on_procs):
        self.on_procs = on_procs

    def stop_watch(self):
        pass

    def tap_source(self, detected, system, on_audio, on_event=None):
        src = FakeSource(on_audio, self.them, rate=48000)
        self.taps.append((detected, system, src))
        return src

    def window_titles(self, bundles):
        return ["Acme Lofts: Lobby panel"]


def make_manager(tmp_path, monkeypatch, *, them=None, me=None, engine="groq", stt="Hello there."):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    st = SettingsStore(tmp_path / "settings.json")
    st.set("calls_dir", str(tmp_path / "Calls"))
    st.set("calls_engine", engine)
    requests = []

    def api(request):
        requests.append(request.url.path)
        reply = stt(len(requests)) if callable(stt) else stt
        return httpx.Response(200, json={"text": reply})

    notes, copied, mics = [], [], []

    def mic_factory(device, on_audio, app_dir=None):
        src = FakeSource(on_audio, me)
        mics.append(src)
        return src

    platform = FakeCallPlatform(them=them)
    mgr = CallManager(st, platform, notify=lambda t, m: notes.append((t, m)), copy=copied.append,
                      pool=E.HttpPool(transport=httpx.MockTransport(api)), mic_factory=mic_factory,
                      app_dir=tmp_path)
    mgr.detector.start_delay = mgr.detector.end_delay = 0.0
    return mgr, st, platform, notes, copied, mics, requests


def wait_for(cond, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_detected_call_is_recorded_saved_and_copied(tmp_path, monkeypatch):
    monkeypatch.setattr("scribe_plus.calls.manager.GROQ_MIN_INTERVAL_S", 0.0)
    them = np.repeat(np.concatenate([quiet(2.5), speech(1.5), quiet(1.2)]), 3)
    me = np.concatenate([quiet(0.3), speech(1.5), quiet(3.0)])
    # The other side's audio is transcribed first here, but they spoke second:
    # the transcript goes by when things were said, not when they were transcribed.
    replies = iter(["Hey Will, the panel is installed.", "Hi, it's Will from ButterflyMX."])
    mgr, st, platform, notes, copied, mics, requests = make_manager(
        tmp_path, monkeypatch, them=them, me=me, stt=lambda n: next(replies))
    mgr._on_procs([DIALER])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.recording)
    assert mgr.status()["name"] == "Groove OmniDialer"
    detected, system, tap = platform.taps[0]
    assert system is False and detected.bundles == ["com.electron.dialer"]      # only the dialer's sound
    assert wait_for(lambda: mgr.status()["lines"] == 2)
    mgr._on_procs([])                                                           # hung up
    mgr._tick(time.monotonic())
    assert wait_for(lambda: not mgr.recording and copied)
    assert tap.stopped and mics[0].stopped
    text = (tmp_path / "Calls" / "latest.md").read_text(encoding="utf-8")
    assert copied == [text]
    assert "# Call: Groove OmniDialer" in text and "Status: ended" in text
    assert "Window: Acme Lofts: Lobby panel" in text
    assert "Me: Hi, it's Will from ButterflyMX." in text and "Them: Hey Will, the panel is installed." in text
    assert text.index("Me: Hi") < text.index("Them: Hey")
    assert mgr.last_file().name.endswith("Groove OmniDialer.md") and mgr.last_file().read_text(encoding="utf-8") == text
    assert [t for t, _m in notes] == ["Recording a call", "Call transcript ready"]
    assert "2 lines saved and copied" in notes[1][1]
    assert len(requests) == 2


def test_stopping_from_the_menu_does_not_restart_while_the_app_keeps_the_mic(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch)
    mgr._on_procs([DIALER])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.recording)
    mgr.stop()
    assert wait_for(lambda: not mgr.recording and not mgr._busy)
    for _ in range(5):
        mgr._tick(time.monotonic())
        time.sleep(0.02)
    assert not mgr.recording and len(platform.taps) == 1
    mgr._on_procs([])                                    # the app finally lets go ...
    mgr._tick(time.monotonic())
    mgr._on_procs([DIALER])                              # ... and a new call starts
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.recording) and len(platform.taps) == 2


def test_manual_recording_uses_all_sound(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch)
    mgr.start_manual()
    assert wait_for(lambda: mgr.recording)
    detected, system, _tap = platform.taps[0]
    assert detected is None and system is True and mgr.status()["manual"]
    mgr.stop()
    assert wait_for(lambda: not mgr.recording)
    assert notes[-1][0] == "Call ended, nothing transcribed"
    assert not (tmp_path / "Calls" / "latest.md").exists() or "was not recorded" in (tmp_path / "Calls" / "latest.md").read_text()


def test_calls_never_go_to_the_cloud_unless_chosen(tmp_path, monkeypatch):
    # "auto" means on this computer. With no on-device engine installed the call
    # is not recorded at all, rather than quietly sent to Groq.
    mgr, st, platform, notes, copied, mics, requests = make_manager(tmp_path, monkeypatch, engine="auto")
    monkeypatch.setattr(E.MLX_WHISPER, "available", lambda: False)
    monkeypatch.setattr(E.LOCAL_WHISPER, "available", lambda: False)
    assert mgr.resolve_engine()[0] == ""
    mgr._on_procs([DIALER])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: notes)
    time.sleep(0.1)
    assert not mgr.recording and platform.taps == [] and requests == []
    assert notes[0][0] == "Call not recorded" and "isn't installed" in notes[0][1]
    for _ in range(5):                                   # and it doesn't nag on every tick
        mgr._tick(time.monotonic())
    time.sleep(0.1)
    assert len(notes) == 1
    monkeypatch.setattr(E.MLX_WHISPER, "available", lambda: True)
    assert mgr.resolve_engine()[:2] == ("mlx", "mlx-community/whisper-large-v3-turbo")


def test_detection_can_be_turned_off_and_silence_is_explained(tmp_path, monkeypatch):
    silent_them = np.zeros(48000 * 3, dtype=np.float32)
    mgr, st, platform, notes, copied, mics, _req = make_manager(
        tmp_path, monkeypatch, them=silent_them, me=quiet(3.0))
    st.set("calls_enabled", False)
    mgr._on_procs([DIALER])
    mgr._tick(time.monotonic())
    time.sleep(0.1)
    assert not mgr.recording
    st.set("calls_enabled", True)
    mgr._on_procs([])
    mgr._tick(time.monotonic())
    mgr._on_procs([DIALER])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.recording)
    mgr._on_procs([])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: not mgr.recording and len(notes) >= 2)
    assert notes[-1][0] == "Call ended, nothing transcribed"
    assert "System Audio Recording" in notes[-1][1]      # the likely cause, in plain words
    assert "System Audio Recording" in mgr.last_file().read_text(encoding="utf-8")
    assert copied == []


def test_settings_file_gets_the_call_defaults(tmp_path):
    st = SettingsStore(tmp_path / "settings.json")
    assert st.get("calls_engine") == "auto" and st.get("calls_copy_on_end") is True
    names = [a["name"] for a in st.get("calls_apps")]
    assert names[0] == "Groove OmniDialer" and "Zoom" in names
    assert [a["name"] for a in st.get("calls_apps") if not a["enabled"]] == ["Google Meet (Chrome)", "Slack huddles"]
