"""
Decides when a call starts and when it ends.

The Mac helper reports which apps have the microphone open. A call is "a
watched app has held the microphone for a moment"; it is over once the app
has let go for a moment. The short waits ride out blips (an app testing the
mic, a device switching) without adding much to how fast a call closes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Apps to watch. `bundles` are matched as prefixes of the app's bundle id
# (helper processes share their app's prefix); `paths` are fragments of the
# app's location, for apps whose bundle id varies. Editable in settings.json.
DEFAULT_APPS: list[dict] = [
    {"name": "Groove OmniDialer", "bundles": ["com.electron.dialer"], "paths": ["omnidialer"],
     "enabled": True},
    {"name": "Zoom", "bundles": ["us.zoom.xos"], "paths": [], "enabled": True},
    {"name": "Microsoft Teams", "bundles": ["com.microsoft.teams2", "com.microsoft.teams"],
     "paths": [], "enabled": True},
    {"name": "Google Meet (Chrome)", "bundles": ["com.google.Chrome"], "paths": [], "enabled": False},
    {"name": "Slack huddles", "bundles": ["com.tinyspeck.slackmacgap"], "paths": [], "enabled": False},
]


def app_root(path: str) -> str:
    """'/Applications/X.app/Contents/Frameworks/X Helper.app/...' -> '/Applications/X.app/'."""
    i = (path or "").find(".app/")
    return path[: i + 5] if i >= 0 else ""


def matches(app: dict, proc: dict) -> bool:
    bundle = str(proc.get("bundle") or "").lower()
    path = str(proc.get("path") or "").lower()
    if any(b and bundle.startswith(str(b).lower()) for b in app.get("bundles") or []):
        return True
    return any(p and str(p).lower() in path for p in app.get("paths") or [])


@dataclass
class Detected:
    app: dict
    procs: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return str(self.app.get("name") or "Call")

    @property
    def bundles(self) -> list[str]:
        return [str(b) for b in self.app.get("bundles") or [] if b]

    @property
    def roots(self) -> list[str]:
        roots: list[str] = []
        for p in self.procs:
            r = app_root(str(p.get("path") or ""))
            if r and r not in roots:
                roots.append(r)
        return roots


class CallDetector:
    def __init__(self, apps: list[dict], *, start_delay: float = 1.0, end_delay: float = 1.5) -> None:
        self.apps = apps
        self.start_delay, self.end_delay = start_delay, end_delay
        self._procs: list[dict] = []
        self._since: dict[str, float] = {}     # app name -> when its mic use began
        self._active: Detected | None = None
        self._quiet_since: float | None = None

    @property
    def active(self) -> Detected | None:
        return self._active

    def using_mic(self, app: dict) -> list[dict]:
        return [p for p in self._procs if p.get("input") and matches(app, p)]

    def all_procs(self, app: dict) -> list[dict]:
        return [p for p in self._procs if matches(app, p)]

    def update(self, procs: list[dict], now: float) -> None:
        """The latest report from the helper (only apps with audio open)."""
        self._procs = list(procs or [])
        for app in self.apps:
            name = str(app.get("name"))
            if app.get("enabled", True) and self.using_mic(app):
                self._since.setdefault(name, now)
            else:
                self._since.pop(name, None)

    def poll(self, now: float) -> tuple[str, Detected] | None:
        """("start", call) or ("stop", call) when something changed, else None."""
        if self._active is None:
            for app in self.apps:
                began = self._since.get(str(app.get("name")))
                if began is not None and now - began >= self.start_delay:
                    self._active = Detected(app, self.all_procs(app))
                    self._quiet_since = None
                    return "start", self._active
            return None
        if self.using_mic(self._active.app):
            self._quiet_since = None
            return None
        if self._quiet_since is None:
            self._quiet_since = now
        if now - self._quiet_since >= self.end_delay:
            ended, self._active, self._quiet_since = self._active, None, None
            self._since.pop(ended.name, None)
            return "stop", ended
        return None

    def reset(self) -> None:
        self._active, self._quiet_since = None, None
