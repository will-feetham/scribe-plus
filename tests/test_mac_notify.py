"""Mac notifications: outside Scribe Plus.app they fall back to AppleScript."""
from scribe_plus.mac import notify


def test_outside_the_app_bundle_uses_applescript(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(notify, "in_app_bundle", lambda: False)
    n = notify.Notifier()
    assert n.status == "applescript"
    n.post('Say "hi"', "Ready. Hold Right Option and talk.")
    script = calls[0][2]
    assert calls[0][:2] == ["osascript", "-e"]
    assert 'with title "Say \\"hi\\""' in script
    assert script.startswith('display notification "Ready. Hold Right Option and talk."')


def test_turned_off_means_nothing_is_shown(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(notify, "in_app_bundle", lambda: False)
    n = notify.Notifier()
    n._center, n.status = object(), "denied"
    n.post("Scribe Plus", "Ready")
    assert calls == []
