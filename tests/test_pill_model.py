"""The pill's animation, independent of any window toolkit."""
from scribe_plus.pill_model import BARS, PillAnimator


def run(anim, start, seconds, fps=60):
    t, frame = start, None
    for _ in range(int(seconds * fps)):
        t += 1 / fps
        frame = anim.step(t, 1 / fps)
        if frame is None:
            return t, None
    return t, frame


def kinds(frame):
    return [item[0] for item in frame.items]


def test_grows_from_a_dot_and_fades_in():
    a = PillAnimator(1.0)
    assert a.set_state("recording", 0.0, mode="dictate") is True  # appearing
    first = a.step(1 / 60, 1 / 60)
    assert first.width < a.w_target and first.alpha < 0.5
    t, frame = run(a, 1 / 60, 0.6)
    assert abs(frame.width - a.w_target) <= 2 and frame.alpha > 0.9
    assert kinds(frame).count("bar") == BARS and kinds(frame)[0] == "capsule"


def test_levels_drive_the_bars_and_pauses_keep_moving():
    a = PillAnimator(1.0)
    a.set_state("recording", 0.0)
    for v in (1.0,) * BARS:
        a.set_state("level", 0.0, value=v)
    _, loud = run(a, 0.0, 0.4)
    loud_h = max(i[3] - i[2] for i in loud.items if i[0] == "bar")
    for _ in range(BARS):
        a.set_state("level", 0.4, value=0.0)
    _, quiet = run(a, 0.4, 0.4)
    quiet_h = [i[3] - i[2] for i in quiet.items if i[0] == "bar"]
    assert loud_h > max(quiet_h) * 2
    assert len({round(h, 2) for h in quiet_h}) > 1, "idle wave keeps the bars moving"


def test_labels_widen_the_pill():
    a = PillAnimator(1.0, measure=lambda s: 7 * len(s))
    a.set_state("recording", 0.0, mode="dictate")
    plain = a.w_target
    a.set_state("recording", 0.0, mode="command")
    assert a.w_target > plain
    a.set_state("recording", 0.0, mode="handsfree")
    _, f = run(a, 0.0, 0.5)
    assert "dot" in kinds(f) and "text" in kinds(f)


def test_done_draws_a_check_then_hides_itself():
    a = PillAnimator(1.0)
    a.set_state("done", 0.0)
    _, f = run(a, 0.0, 0.3)
    assert "polyline" in kinds(f)
    t, f = run(a, 0.3, 2.0)
    assert f is None and a.state == "hidden" and not a.closing


def test_error_shakes_then_fades():
    a = PillAnimator(1.0)
    a.set_state("error", 0.0, message="No speech heard")
    shakes = []
    t = 0.0
    for _ in range(20):
        t += 1 / 60
        shakes.append(a.step(t, 1 / 60).shake)
    assert any(s != 0 for s in shakes)
    t, f = run(a, t, 1.0)
    assert f is not None and f.shake == 0 and ("text" in kinds(f))
    t, f = run(a, t, 3.0)
    assert f is None


def test_idle_closes_and_reopening_mid_close_cancels_it():
    a = PillAnimator(1.0)
    a.set_state("processing", 0.0)
    run(a, 0.0, 0.5)
    a.set_state("idle", 0.5)
    assert a.closing
    a.step(0.52, 0.02)
    assert a.set_state("recording", 0.53) is True and not a.closing
    _, f = run(a, 0.53, 0.5)
    assert f is not None and f.alpha > 0.9


def test_scale_and_hidden_noops():
    a = PillAnimator(2.0)
    assert a.h == 68 and a.font_px == 26
    a.set_state("idle", 0.0)  # closing a hidden pill does nothing
    assert a.state == "hidden" and not a.closing
    assert a.set_state("level", 0.0, value=0.5) is False
    assert a.set_state("bogus", 0.0) is False
