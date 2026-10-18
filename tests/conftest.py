"""
Test setup. Runs on any OS: Windows-only pieces are faked, and the data
folder points at a temp dir so tests never touch a real install.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="scribe-plus-tests-"))
os.environ["SCRIBE_PLUS_HOME"] = str(_TMP / "plus")
os.environ["XDG_CONFIG_HOME"] = str(_TMP / "config")
os.environ["APPDATA"] = str(_TMP / "appdata")
for k in ("GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)

# scribe_core imports sounddevice (PortAudio) at module level; the tests
# never record, so a stub is enough.
if "sounddevice" not in sys.modules:
    try:
        import sounddevice  # noqa: F401
    except Exception:
        sys.modules["sounddevice"] = types.ModuleType("sounddevice")
