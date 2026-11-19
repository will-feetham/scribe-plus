"""A whole call on a GitHub Mac runner, using the installed app's own code.

    <app venv python> mac_call_e2e.py APP_DIR CALLER_PYTHON SPEECH_WAV MLX_MODEL

A stand-in calling app (fake_call.py, run by a different Python) opens the
microphone and plays a spoken sentence. Scribe Plus's real call manager must
notice it, record that app's sound and the microphone, transcribe on the
machine and write the transcript.

Exit code 0 when the spoken sentence comes out as a "Them" line.
"""
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

app_dir, caller_python, speech_wav, mlx_model = sys.argv[1:5]
sys.path.insert(0, app_dir)
os.chdir(app_dir)
logging.basicConfig(level=logging.INFO, format="%(relativeCreated)6d %(name)s: %(message)s",
                    stream=sys.stdout)

import numpy as np  # noqa: E402

from scribe_plus import engines  # noqa: E402
from scribe_plus.calls.manager import CallManager  # noqa: E402
from scribe_plus.mac.calls import MacCallPlatform  # noqa: E402
from scribe_plus.settings import SettingsStore  # noqa: E402

EXPECTED = ["lobby", "panel", "installed", "tuesday", "keys", "office"]
results: list[str] = []


def result(text: str) -> None:
    """Printed now, and again at the end where the run notice shows it."""
    results.append(text)
    print("RESULT", text, flush=True)


def finish(passed: bool) -> None:
    print("===== summary =====")
    for line in results:
        print(line)
    print("PASS" if passed else "FAIL")
    sys.exit(0 if passed else 1)


work = Path(tempfile.mkdtemp(prefix="call-e2e-"))
caller = os.path.realpath(caller_python)

# Which speech engine works here? MLX needs a GPU the runner may not expose.
engine, model = "", ""
try:
    t = time.perf_counter()
    engines.MLX_WHISPER.transcribe(np.zeros(16000, dtype=np.float32), model=mlx_model,
                                   language="en", prompt="")
    engine, model = "mlx", mlx_model
    result(f"engine: MLX works on this runner (first run {time.perf_counter() - t:.1f} s)")
except Exception as exc:
    result(f"engine: MLX not usable here ({type(exc).__name__}: {str(exc)[:160]})")
    if engines.LOCAL_WHISPER.available():
        engine, model = "local", "tiny.en"
        result("engine: using faster-whisper tiny.en instead")
if not engine:
    result("no on-device speech engine on this runner; nothing to test")
    finish(True)

settings = SettingsStore(work / "settings.json")
settings.set("calls_dir", str(work / "Calls"))
settings.set("calls_engine", engine)
settings.set_nested("calls_models", engine, model)
settings.set("calls_apps", [{"name": "Test caller", "bundles": [], "paths": [caller], "enabled": True}])

notes: list[tuple[str, str]] = []
copied: list[str] = []
manager = CallManager(settings, MacCallPlatform(Path(app_dir)),
                      notify=lambda title, message: notes.append((title, message)),
                      copy=copied.append, pool=engines.HttpPool(), app_dir=Path(app_dir))
ok, why = manager.available()
result(f"available: {ok} {why}".rstrip())
if not ok:
    finish(False)
manager.start()
time.sleep(1.0)

call = subprocess.Popen([caller, str(Path(__file__).with_name("fake_call.py")), speech_wav])
noticed = False
deadline = time.time() + 20
while time.time() < deadline and not manager.recording:
    time.sleep(0.1)
if manager.recording:
    noticed = True
    result("detected: the call was noticed (" + manager.status()["name"] + ")")
else:
    result("detected: NO, the call was never noticed")
call.wait(60)
hung_up = time.time()
deadline = time.time() + 60
while time.time() < deadline and (manager.recording or manager._busy):
    time.sleep(0.05)
result(f"closed: {time.time() - hung_up:.1f} s after the caller hung up")
manager.shutdown()

for title, message in notes:
    result(f"notification: {title}: {message}")
path = manager.last_file()
text = path.read_text(encoding="utf-8") if path else ""
print("----- transcript -----")
print(text or "(no transcript file)")
print("----------------------")

them = " ".join(line for line in text.lower().splitlines() if "] them:" in line)
found = [w for w in EXPECTED if w in them]
result(f"them: {them[:200] or '(nothing)'}")
result(f"words heard from the other side: {found}")
result(f"microphone recorded: {'microphone was not recorded' not in text}")
result(f"copied to the clipboard: {bool(copied) and copied[-1] == text}")
finish(noticed and len(found) >= 3 and "microphone was not recorded" not in text
       and bool(copied) and "Status: ended" in text)
