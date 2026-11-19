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
    said = Line(10.0, "Them", "Can you send me the install date for the building?", 13.0)
    tr.add(said)
    echo = Line(10.4, "Me", "can you send me the install date for the building", 13.2)
    assert tr.is_echo(echo)
    assert not tr.is_echo(Line(14.0, "Me", "Sure, it's scheduled for the fifth of December.", 16.0))
    assert not tr.is_echo(Line(60.0, "Me", "Can you send me the install date for the building?", 63.0))
    assert not tr.is_echo(Line(10.4, "Me", "Yes.", 10.8))
    # Reading something back is not an echo: it comes after, not at the same moment.
    assert not tr.is_echo(Line(13.4, "Me", "Can you send me the install date for the building?", 16.0))
    # The microphone's copy can be transcribed first; it is found from the other side too.
    tr2 = Transcript("Zoom", datetime(2026, 11, 19, 9, 0))
    tr2.add(echo)
    assert tr2.echoes_of(said) == [echo] and tr2.remove(echo) and tr2.lines() == []
    readback = Line(13.4, "Me", "Can you send me the install date for the building?", 16.0)
    tr2.add(readback)
    assert tr2.echoes_of(said) == []


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


def test_two_calls_in_one_minute_never_share_a_file(tmp_path):
    # Neither has written yet (nothing is written before the first line).
    when = datetime(2026, 11, 19, 11, 42)
    first = TranscriptFiles(tmp_path, Transcript("Groove OmniDialer", when))
    second = TranscriptFiles(tmp_path, Transcript("Groove OmniDialer", when))
    assert first.path != second.path and second.path.name.endswith("(2).md")
    second.release()                                    # that one ended with nothing said
    third = TranscriptFiles(tmp_path, Transcript("Groove OmniDialer", when))
    assert third.path == second.path


def test_latest_belongs_to_the_newest_call(tmp_path):
    older_tr = Transcript("Groove OmniDialer", datetime(2026, 11, 19, 11, 42))
    newer_tr = Transcript("Groove OmniDialer", datetime(2026, 11, 19, 11, 50))
    older, newer = TranscriptFiles(tmp_path, older_tr), TranscriptFiles(tmp_path, newer_tr)
    older_tr.add(Line(1.0, "Me", "First call.", 2.0))
    older.write()
    assert "First call." in older.latest.read_text(encoding="utf-8")
    newer_tr.add(Line(1.0, "Me", "Second call.", 2.0))
    newer.write()
    older_tr.add(Line(9.0, "Them", "A slow last line of the first call.", 11.0))
    older.write()                                       # the first call's transcript finishing late
    assert "A slow last line" in older.path.read_text(encoding="utf-8")
    latest = older.latest.read_text(encoding="utf-8")
    assert "Second call." in latest and "First call." not in latest


# -- the recorder ------------------------------------------------------------------------


def test_noise_text_and_normalize():
    assert is_noise_text("you") and is_noise_text("[BLANK_AUDIO]") and is_noise_text("Thanks for watching!")
    assert not is_noise_text("Okay.") and not is_noise_text("Thank you.") and not is_noise_text("Bye.")
    assert not is_noise_text("42.") and not is_noise_text("7") and not is_noise_text("555-0199")
    assert is_noise_text("...") and is_noise_text("a")
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
        if len(calls) <= 2:                               # the first sentence fails, and fails again
            raise RuntimeError("model not installed")
        return "Second sentence."

    rec, tr, files = make_recorder(tmp_path, transcribe, on_error=errors.append, retry_after_s=0.0)
    feed_realtime(rec, "Me", np.concatenate([quiet(0.5), speech(1.0), quiet(1.5)]), RATE, 0.0)
    assert settle(rec) and len(errors) == 1 and len(calls) == 2
    assert not files.path.exists()                        # nothing said yet, so no file yet
    feed_realtime(rec, "Me", np.concatenate([speech(1.0), quiet(1.5)]), RATE, 3.0)
    assert rec.finish(timeout=5.0)
    assert len(errors) == 1 and [ln.text for ln in tr.lines()] == ["Second sentence."]
    assert "could not be transcribed (model not installed)" in files.latest.read_text(encoding="utf-8")


def test_a_passing_failure_is_tried_again_and_nothing_is_lost(tmp_path):
    errors, calls = [], []

    def transcribe(samples, speaker):
        calls.append(speaker)
        if len(calls) == 1:
            raise RuntimeError("HTTP 429: slow down")
        return "The keys are in the office."

    rec, tr, _files = make_recorder(tmp_path, transcribe, on_error=errors.append, retry_after_s=0.0)
    feed_realtime(rec, "Them", np.concatenate([quiet(0.5), speech(1.0), quiet(1.5)]), RATE, 0.0)
    assert rec.finish(timeout=5.0)
    assert errors == [] and tr.notes == [] and [ln.text for ln in tr.lines()] == ["The keys are in the office."]


def test_a_call_with_nothing_said_leaves_no_file(tmp_path):
    rec, _tr, files = make_recorder(tmp_path, lambda samples, speaker: "you")
    feed_realtime(rec, "Me", np.concatenate([quiet(0.5), speech(1.0), quiet(1.5)]), RATE, 0.0)
    assert rec.finish(timeout=5.0)
    assert list(tmp_path.iterdir()) == [] and not files.latest.exists()


def test_numbers_and_read_backs_are_kept(tmp_path):
    replies = {"Them": iter(["The gate code is 4 4 1 9.", "42."]),
               "Me": iter(["The gate code is 4 4 1 9."])}
    rec, tr, _files = make_recorder(tmp_path, lambda samples, speaker: next(replies[speaker]))
    feed_realtime(rec, "Them", np.concatenate([quiet(0.5), speech(2.0), quiet(1.5)]), RATE, 0.0)
    feed_realtime(rec, "Me", np.concatenate([quiet(3.0), speech(2.0), quiet(1.5)]), RATE, 0.0)     # read back
    feed_realtime(rec, "Them", np.concatenate([quiet(3.0), speech(0.8), quiet(1.5)]), RATE, 4.0)
    assert rec.finish(timeout=5.0)
    assert [(ln.speaker, ln.text) for ln in tr.lines()] == [
        ("Them", "The gate code is 4 4 1 9."), ("Me", "The gate code is 4 4 1 9."), ("Them", "42.")]


def test_an_echo_transcribed_first_is_taken_back_out(tmp_path):
    gate = threading.Event()

    def transcribe(samples, speaker):
        if speaker == "Them":
            gate.wait(5.0)                                # the other side's text arrives second
        return "Can you confirm the install date for the lobby panel?"

    rec, tr, _files = make_recorder(tmp_path, transcribe)
    feed_realtime(rec, "Me", np.concatenate([quiet(0.6), speech(2.0), quiet(1.5)]), RATE, 0.0)
    assert settle(rec) and [ln.speaker for ln in tr.lines()] == ["Me"]
    feed_realtime(rec, "Them", np.concatenate([quiet(0.5), speech(2.0), quiet(1.5)]), RATE, 0.0)
    gate.set()
    assert rec.finish(timeout=5.0)
    assert [ln.speaker for ln in tr.lines()] == ["Them"] and rec.summary()["lines"] == 1


def test_one_bad_sample_does_not_deafen_a_side(tmp_path):
    rec, tr, _files = make_recorder(tmp_path, lambda samples, speaker: "Still here.")
    bad = quiet(0.5)
    bad[100] = np.nan
    bad[200] = np.inf
    feed_realtime(rec, "Them", bad, RATE, 0.0)
    feed_realtime(rec, "Them", np.concatenate([speech(1.5), quiet(1.5)]), RATE, 0.5)
    assert rec.finish(timeout=5.0)
    assert [ln.text for ln in tr.lines()] == ["Still here."]
    assert np.isfinite(rec.channels["Them"].peak)


def test_requests_are_paced_but_a_quick_exchange_goes_out_at_once(tmp_path, monkeypatch):
    # Groq's limit is 20 requests a minute: three at once, then one every 3.5 s.
    import types

    from scribe_plus.calls import recorder as recorder_module
    rec, _tr, _files = make_recorder(tmp_path, lambda samples, speaker: "", min_interval_s=lambda: 3.5, burst=3)
    now, sent = [100.0], []

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(recorder_module, "time", types.SimpleNamespace(
        monotonic=lambda: now[0], sleep=sleep, perf_counter=time.perf_counter))
    rec._tokens_at = now[0]
    for _ in range(45):
        rec._pace()
        sent.append(now[0])
    assert sent[:5] == [100.0, 100.0, 100.0, 103.5, 107.0]
    assert max(sum(1 for t in sent if start <= t < start + 60.0) for start in sent) == 20
    now[0] += 60.0                                        # a quiet minute earns the three back, no more
    rec._pace(), rec._pace(), rec._pace()
    quiet_until = now[0]
    rec._pace()
    assert now[0] == quiet_until + 3.5
    monkeypatch.undo()
    assert rec.finish(timeout=2.0)


def test_a_slow_model_catches_up_in_batches_and_a_late_finish_updates_the_file(tmp_path):
    release, started, sizes, done = threading.Event(), threading.Event(), [], []

    def transcribe(samples, speaker):
        sizes.append(len(samples) / RATE)
        if len(sizes) >= 2:
            started.set()
            release.wait(5.0)
        return f"part {len(sizes)}"

    rec, tr, files = make_recorder(tmp_path, transcribe)
    rec.on_done = lambda: done.append(True)
    feed_realtime(rec, "Me", np.concatenate([quiet(0.5), speech(1.0), quiet(1.2)]), RATE, 0.0)
    assert settle(rec)
    feed_realtime(rec, "Me", np.concatenate([speech(1.0), quiet(1.2)]), RATE, 5.0)
    assert started.wait(5.0)                            # the model is now busy with the second one
    rest = np.concatenate([np.concatenate([speech(1.0), quiet(1.2)]) for _ in range(3)])
    feed_realtime(rec, "Me", rest, RATE, 7.2)
    assert rec.finish(timeout=0.3) is False             # ... and still is at hang-up
    text = files.latest.read_text(encoding="utf-8")
    assert "still being transcribed" in text and "part 1" in text and not done
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and not done:
        time.sleep(0.02)
    text = files.latest.read_text(encoding="utf-8")
    assert done and "still being transcribed" not in text and "part 3" in text
    assert len(sizes) == 3 and sizes[2] > 3.0           # the three waiting utterances went as one request


def test_closing_the_app_mid_call_still_ends_the_transcript(tmp_path):
    release = threading.Event()

    def transcribe(samples, speaker):
        if release.is_set():
            return "never used"
        release.wait(3.0) if getattr(transcribe, "second", False) else None
        transcribe.second = True
        return "Hello there."

    rec, tr, files = make_recorder(tmp_path, transcribe)
    feed_realtime(rec, "Me", np.concatenate([quiet(0.5), speech(1.0), quiet(1.2)]), RATE, 0.0)
    assert settle(rec)
    feed_realtime(rec, "Me", np.concatenate([speech(1.0), quiet(1.2)]), RATE, 5.0)
    time.sleep(0.1)
    assert rec.finish(timeout=0.2, final=True) is False
    text = files.latest.read_text(encoding="utf-8")
    assert "Status: ended" in text and "closed before the last part" in text
    release.set()


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


def test_audio_that_was_only_held_up_keeps_its_real_time():
    # The reader stalls for 3 s, then the audio it missed arrives in a rush:
    # nothing was lost, so the clock must not stay 3 s ahead.
    ch = Channel("Them", t0=1000.0)
    for i in range(10):
        ch.push(quiet(0.1), RATE, arrived=1000.0 + 0.1 * (i + 1))
    jobs = []
    for i in range(45):                                  # 4.5 s of audio in under half a second
        piece = speech(0.1) if 15 <= i < 26 else quiet(0.1)
        jobs += ch.push(piece, RATE, arrived=1004.0 + 0.01 * (i + 1))
    assert len(jobs) == 1 and 2.0 <= jobs[0].t <= 3.1    # said 2.5 s into the stream, not 5.4


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
    d.update([dict(DIALER, input=False)], now=200.0)     # hung up; playing a short tone
    assert d.poll(200.1) is None
    d.update([], now=200.8)
    assert d.poll(201.0) is None
    event = d.poll(201.7)
    assert event and event[0] == "stop"
    assert d.poll(205.0) is None and d.active is None


def test_an_app_still_playing_sound_gets_longer_before_the_call_counts_as_over():
    d = CallDetector(DEFAULT_APPS, start_delay=0.6, end_delay=1.5, sound_hold=4.0)
    d.update([DIALER], now=100.0)
    first = d.poll(101.0)[1]
    d.update([dict(DIALER, input=False)], now=200.0)     # let go of the mic, the other side still plays
    assert d.poll(201.7) is None and d.poll(203.5) is None
    d.update([DIALER], now=203.6)                        # ... and took it back: one call, not two
    assert d.poll(210.0) is None and d.active is first
    d.update([dict(DIALER, input=False)], now=300.0)
    assert d.poll(300.1) is None and d.poll(303.9) is None
    assert d.poll(304.1)[0] == "stop"                    # but not for ever


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

    def __init__(self, on_audio, audio=None, rate=RATE, fail=False, start_delay=0.0):
        self.on_audio, self.audio, self.rate, self.fail = on_audio, audio, rate, fail
        self.start_delay = start_delay
        self.started = self.stopped = False
        self.last_error = ""
        self.fault = ""                  # what problem() reports ("" while healthy)

    def start(self):
        if self.fail:
            raise RuntimeError("helper missing")
        time.sleep(self.start_delay)
        self.started = True
        if self.audio is not None:
            now = time.monotonic()
            block = self.rate // 10
            for i in range(0, len(self.audio), block):
                piece = self.audio[i:i + block]
                self.on_audio(piece, self.rate, now + (i + len(piece)) / self.rate)

    def problem(self):
        return self.fault

    def stop(self, timeout=1.5):
        self.stopped = True


class FakeCallPlatform:
    def __init__(self, them=None, ok=True, start_delay=0.0):
        self.them, self.ok, self.start_delay = them, ok, start_delay
        self.on_procs = None
        self.taps = []
        self.awake = []

    def available(self):
        return (True, "") if self.ok else (False, "call recording needs macOS 14.2 or newer")

    def start_watch(self, on_procs):
        self.on_procs = on_procs

    def stop_watch(self):
        pass

    def tap_source(self, detected, system, on_audio, on_event=None):
        src = FakeSource(on_audio, self.them, rate=48000, start_delay=self.start_delay)
        self.taps.append((detected, system, src))
        return src

    def call_started(self):
        self.awake.append("start")

    def call_ended(self):
        self.awake.append("end")

    def window_titles(self, bundles):
        return ["Acme Lofts: Lobby panel"]


def make_manager(tmp_path, monkeypatch, *, them=None, me=None, engine="groq", stt="Hello there.",
                 stt_delay=0.0, start_delay=0.0):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr("scribe_plus.calls.manager.GROQ_MIN_INTERVAL_S", 0.0)
    st = SettingsStore(tmp_path / "settings.json")
    st.set("calls_dir", str(tmp_path / "Calls"))
    st.set("calls_engine", engine)
    requests = []

    def api(request):
        requests.append(request.url.path)
        reply = stt(len(requests)) if callable(stt) else stt
        time.sleep(stt_delay)
        return httpx.Response(200, json={"text": reply})

    notes, copied, mics = [], [], []

    def mic_factory(device, on_audio, app_dir=None):
        src = FakeSource(on_audio, me)
        mics.append(src)
        return src

    platform = FakeCallPlatform(them=them, start_delay=start_delay)
    mgr = CallManager(st, platform, notify=lambda t, m: notes.append((t, m)), copy=copied.append,
                      pool=E.HttpPool(transport=httpx.MockTransport(api)), mic_factory=mic_factory,
                      app_dir=tmp_path)
    mgr.detector.start_delay = mgr.detector.end_delay = 0.0
    return mgr, st, platform, notes, copied, mics, requests


def ring(mgr, procs):
    """What the helper reports, and one pass of the manager's loop."""
    mgr._on_procs(procs)
    mgr._tick(time.monotonic())


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
    assert wait_for(lambda: mgr.idle and copied)
    assert tap.stopped and mics[0].stopped and platform.awake == ["start", "end"]
    text = (tmp_path / "Calls" / "latest.md").read_text(encoding="utf-8")
    assert copied == [text]
    assert "# Call: Groove OmniDialer" in text and "Status: ended" in text
    assert "Window: Acme Lofts: Lobby panel" in text
    assert "Me: Hi, it's Will from ButterflyMX." in text and "Them: Hey Will, the panel is installed." in text
    assert text.index("Me: Hi") < text.index("Them: Hey")
    assert mgr.last_file().name.endswith("Groove OmniDialer.md") and mgr.last_file().read_text(encoding="utf-8") == text
    assert [t for t, _m in notes] == ["Recording a call", "Call transcript ready"]
    assert notes[1][1].startswith("Groove OmniDialer: 2 lines saved and copied, ")
    assert len(requests) == 2


def test_stopping_from_the_menu_does_not_restart_while_the_app_keeps_the_mic(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch)
    mgr._on_procs([DIALER])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.recording)
    mgr.stop()
    assert wait_for(lambda: mgr.idle)
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
    monkeypatch.setattr("scribe_plus.calls.manager.QUIET_UNDER_S", 0.0)
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch)
    mgr.start_manual()
    assert wait_for(lambda: mgr.recording)
    detected, system, _tap = platform.taps[0]
    assert detected is None and system is True and mgr.status()["manual"]
    mgr.stop()
    assert wait_for(lambda: mgr.idle)
    assert notes[-1][0] == "Call ended, nothing transcribed"
    assert not (tmp_path / "Calls").exists() and mgr.last_file() is None      # nothing said, no file


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


def test_a_momentary_mic_use_ends_quietly(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch)
    mgr._on_procs([DIALER])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.recording)
    mgr._on_procs([])
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.idle)
    assert [t for t, _m in notes] == ["Recording a call"] and copied == []
    assert not (tmp_path / "Calls").exists()


def test_detection_can_be_turned_off_and_silence_is_explained(tmp_path, monkeypatch):
    monkeypatch.setattr("scribe_plus.calls.manager.QUIET_UNDER_S", 0.0)
    silent_them = np.zeros(48000 * 3, dtype=np.float32)
    mgr, st, platform, notes, copied, mics, _req = make_manager(
        tmp_path, monkeypatch, them=silent_them, me=quiet(3.0))
    st.set("calls_enabled", False)
    ring(mgr, [DIALER])
    time.sleep(0.1)
    assert not mgr.recording
    st.set("calls_enabled", True)                        # turned on mid-call: recorded from here
    mgr._tick(time.monotonic())
    assert wait_for(lambda: mgr.recording)
    ring(mgr, [])
    assert wait_for(lambda: mgr.idle and len(notes) >= 2)
    assert notes[-1][0] == "Call ended, nothing transcribed"
    assert "System Audio Recording" in notes[-1][1]      # the likely cause, in plain words
    assert copied == [] and mgr.last_file() is None


def test_the_next_call_is_recorded_even_while_the_last_one_is_being_written_up(tmp_path, monkeypatch):
    # Back-to-back dialing: call 2 starts a moment after call 1 ends, while
    # call 1's last words are still with the speech model.
    them = np.repeat(np.concatenate([quiet(0.5), speech(1.5), quiet(1.2)]), 3)
    mgr, st, platform, notes, copied, mics, _req = make_manager(
        tmp_path, monkeypatch, them=them, stt=lambda n: f"This is call number {n}.", stt_delay=0.6)
    ring(mgr, [DIALER])
    assert wait_for(lambda: mgr.recording)
    ring(mgr, [])                                        # call 1 ends ...
    ring(mgr, [DIALER])                                  # ... and call 2 is on at once
    assert wait_for(lambda: mgr.recording and len(platform.taps) == 2, 2.0)
    assert wait_for(lambda: len(copied) == 1)            # call 1 handed over while call 2 runs
    assert "This is call number 1." in copied[0] and "Status: ended" in copied[0]
    ring(mgr, [])
    assert wait_for(lambda: mgr.idle and len(copied) == 2)
    files = sorted(p.name for p in (tmp_path / "Calls").glob("2*.md"))
    assert len(files) == 2 and any(name.endswith("(2).md") for name in files)
    latest = (tmp_path / "Calls" / "latest.md").read_text(encoding="utf-8")
    assert latest == copied[1] and "This is call number 2." in latest and "call number 1" not in latest
    assert platform.awake == ["start", "end"]            # kept awake across both, let go once
    assert all(src.stopped for _d, _s, src in platform.taps) and all(m.stopped for m in mics)


def test_a_call_that_ends_while_recording_is_still_starting_is_closed(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch, start_delay=0.6)
    ring(mgr, [DIALER])
    time.sleep(0.05)
    assert mgr._busy and not mgr.recording               # the helpers are still starting
    ring(mgr, [])                                        # hung up already
    assert wait_for(lambda: mgr.idle, 3.0)
    assert len(platform.taps) == 1 and platform.taps[0][2].stopped and mics[0].stopped
    assert not mgr.recording


def test_stop_pressed_while_a_manual_recording_is_starting_still_stops_it(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch, start_delay=0.6)
    mgr.start_manual()
    time.sleep(0.05)
    mgr.stop()
    assert wait_for(lambda: mgr.idle, 3.0) and platform.taps[0][2].stopped


def test_a_recording_helper_that_dies_is_started_again_and_the_transcript_says_so(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch)
    ring(mgr, [DIALER])
    assert wait_for(lambda: mgr.recording and not mgr._busy)
    now = time.monotonic()
    mgr._tick(now + 2)
    assert len(platform.taps) == 1                       # healthy: left alone
    platform.taps[0][2].fault = "it stopped by itself, exit 9"
    mgr._tick(now + 4)
    assert len(platform.taps) == 2 and platform.taps[1][2].started
    assert wait_for(lambda: platform.taps[0][2].stopped)
    call_notes = mgr._call.recorder.transcript.notes
    assert len(call_notes) == 1 and "the other side stopped for a moment" in call_notes[0]
    platform.taps[1][2].fault = "it sent no audio for 9 s"
    mgr._tick(now + 6)
    assert len(platform.taps) == 3
    platform.taps[2][2].fault = "it sent no audio for 9 s"
    mgr._tick(now + 8)
    mgr._tick(now + 10)
    assert len(platform.taps) == 3                       # twice is enough
    assert notes[-1][0] == "Call recording problem" and "no longer being recorded" in notes[-1][1]
    assert "could not be started again (it sent no audio for 9 s)" in call_notes[-1]
    assert len(mics) == 1 and mgr.recording              # the microphone side carries on


def test_quitting_mid_call_closes_the_transcript(tmp_path, monkeypatch):
    me = np.concatenate([quiet(0.3), speech(1.5), quiet(3.0)])
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch, me=me)
    ring(mgr, [DIALER])
    assert wait_for(lambda: mgr.recording and mgr.status()["lines"] == 1)
    mgr.shutdown()
    text = (tmp_path / "Calls" / "latest.md").read_text(encoding="utf-8")
    assert "Status: ended" in text and "Me: Hello there." in text
    assert platform.taps[0][2].stopped and mics[0].stopped


def test_a_transcript_that_cannot_be_saved_is_said_so_and_still_copied(tmp_path, monkeypatch):
    me = np.concatenate([quiet(0.3), speech(1.5), quiet(3.0)])
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch, me=me)
    (tmp_path / "blocked").write_text("a file where the folder should be")
    st.set("calls_dir", str(tmp_path / "blocked" / "Calls"))
    ring(mgr, [DIALER])
    assert wait_for(lambda: mgr.recording and mgr.status()["lines"] == 1)
    ring(mgr, [])
    assert wait_for(lambda: mgr.idle)
    assert notes[-1][0] == "Call transcript not saved" and "copied to the clipboard" in notes[-1][1]
    assert len(copied) == 1 and "Me: Hello there." in copied[0] and mgr.last_file() is None


def test_the_engine_is_looked_up_for_every_piece_of_speech(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, requests = make_manager(tmp_path, monkeypatch)
    monkeypatch.setattr("scribe_plus.calls.manager.GROQ_MIN_INTERVAL_S", 3.5)
    run = mgr._transcriber()
    assert run(speech(1.0), "Me") == "Hello there." and len(requests) == 1 and mgr._pace() == 3.5
    monkeypatch.setattr(E.MLX_WHISPER, "available", lambda: True)
    monkeypatch.setattr(E.MLX_WHISPER, "transcribe", lambda samples, **kw: "Transcribed on this Mac.")
    st.set("calls_engine", "mlx")                        # switched back mid-call
    assert run(speech(1.0), "Me") == "Transcribed on this Mac."
    assert len(requests) == 1 and mgr._pace() == 0.0     # nothing more went to the cloud


def test_a_hand_edited_app_list_cannot_break_call_detection(tmp_path, monkeypatch):
    mgr, st, platform, notes, copied, mics, _req = make_manager(tmp_path, monkeypatch)
    mine = {"name": "Softphone", "bundles": ["com.example.softphone"], "enabled": True}
    st.set("calls_apps", ["Zoom", 5, None, mine])
    assert mgr.apps() == [mine]
    ring(mgr, [DIALER])                                  # no longer watched, and nothing raises
    time.sleep(0.05)
    assert not mgr.recording and not mgr._busy
    ring(mgr, [{"pid": 9, "bundle": "com.example.softphone", "path": "/Applications/Softphone.app/x", "input": True}])
    assert wait_for(lambda: mgr.recording) and mgr.status()["name"] == "Softphone"
    st.set("calls_apps", "nonsense")
    assert [a["name"] for a in mgr.apps()][0] == "Groove OmniDialer"


def test_settings_file_gets_the_call_defaults(tmp_path):
    st = SettingsStore(tmp_path / "settings.json")
    assert st.get("calls_engine") == "auto" and st.get("calls_copy_on_end") is True
    names = [a["name"] for a in st.get("calls_apps")]
    assert names[0] == "Groove OmniDialer" and "Zoom" in names
    assert [a["name"] for a in st.get("calls_apps") if not a["enabled"]] == ["Google Meet (Chrome)", "Slack huddles"]


def test_the_engine_menu_lists_only_what_is_useful_on_this_computer():
    from scribe_plus.tray import call_engine_menu
    # Apple silicon, set up for calls: the spare processor engine is not shown at all.
    menu = call_engine_menu({"mlx": True, "local": False}, apple_silicon=True)
    assert [key for _label, key in menu] == ["auto", "mlx", "groq", "openai"]
    assert menu[0][0] == "Automatic: on this Mac (MLX Whisper)" and "not installed" not in str(menu)
    # Intel Mac or Windows: MLX can't run there, so it is not offered.
    menu = call_engine_menu({"mlx": False, "local": True}, apple_silicon=False)
    assert [key for _label, key in menu] == ["auto", "local", "groq", "openai"]
    assert menu[0][0] == "Automatic: on this computer (Whisper on the processor)"
    # Apple silicon where the fast engine failed to install: that is said, once, where it matters.
    menu = call_engine_menu({"mlx": False, "local": False}, apple_silicon=True)
    assert [label for label, _key in menu][:2] == ["Automatic: on this computer (not installed)",
                                                   "On this Mac: MLX Whisper (not installed)"]
    menu = call_engine_menu({"mlx": False, "local": True}, apple_silicon=True)
    assert [key for _label, key in menu] == ["auto", "mlx", "local", "groq", "openai"]
    assert menu[1][0].endswith("(not installed)") and "not installed" not in menu[2][0]
