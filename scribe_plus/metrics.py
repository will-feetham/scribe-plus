"""
The research log: one CSV row per dictation, plus saved audio clips.

metrics.csv opens straight in Excel or Google Sheets. Times are in
milliseconds from the moment the hotkey came up, so `total_ms` is the
release-to-text-on-screen latency, the number that decides how fast
dictation feels.
"""
from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

COLUMNS = [
    "timestamp", "mode", "app", "window_title", "style",
    "audio_s", "speech_s", "gain_db",
    "mic_open_ms", "stop_ms", "prep_ms",
    "stt_engine", "stt_model", "stt_ms",
    "cleanup_engine", "cleanup_model", "cleanup_ms", "tokens_in", "tokens_out",
    "paste_ms", "total_ms", "est_cost_usd",
    "guard", "error", "words_raw", "words_final",
    "raw_text", "final_text", "clip",
]


class MetricsLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, row: dict) -> None:
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new = not self.path.exists() or self.path.stat().st_size == 0
                # utf-8-sig puts a BOM at the top so Excel reads accents right.
                with self.path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
                    if new:
                        w.writeheader()
                    w.writerow({k: _fmt(row.get(k, "")) for k in COLUMNS})
        except Exception as exc:
            log.warning("metrics write failed: %s", exc)

    def write_async(self, row: dict) -> None:
        threading.Thread(target=self.write, args=(row,), daemon=True).start()


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.6f}" if abs(v) < 0.01 and v != 0 else f"{v:.1f}"
    return v


def save_clip(wav: bytes, clips_dir: Path, keep: int) -> str:
    """Save a clip for later benchmarking; keep only the newest `keep`."""
    clips_dir = Path(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + ".wav"
    (clips_dir / name).write_bytes(wav)
    if keep > 0:
        for old in sorted(clips_dir.glob("*.wav"))[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return name


def timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
