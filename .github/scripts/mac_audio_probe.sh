#!/usr/bin/env bash
# Exercises macos/audiotap on a GitHub Mac runner and reports what happened
# as a notice on the run. Never fails the build: runners have no real sound
# hardware and can't answer permission prompts, so this is information only.
set +e
helper="${1:-dist/audiotap}"
work="${RUNNER_TEMP:-/tmp}/audio-probe"
mkdir -p "$work"
out="$work/report.txt"
here="$(cd "$(dirname "$0")" && pwd)"
{
  echo "== macOS"; sw_vers; uname -m
  echo "== audio devices"; system_profiler SPAudioDataType 2>&1 | head -50
  echo "== helper version"; "$helper" version; echo "exit $?"
  echo "== permission rows (copy Microphone grants to System Audio Recording)"
  for db in "$HOME/Library/Application Support/com.apple.TCC/TCC.db" \
            "/Library/Application Support/com.apple.TCC/TCC.db"; do
    sudo sqlite3 "$db" "CREATE TEMP TABLE t AS SELECT * FROM access WHERE service='kTCCServiceMicrophone';
      UPDATE t SET service='kTCCServiceAudioCapture';
      INSERT OR REPLACE INTO access SELECT * FROM t;
      SELECT service, client, auth_value FROM access WHERE service='kTCCServiceAudioCapture';" 2>&1 | head -12
    echo "sqlite exit $? for $db"
  done
  echo "== test tone"
  python3 - "$work/tone.wav" <<'PY'
import math, struct, sys, wave
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 44100)))
                           for i in range(44100 * 6)))
print("tone written")
PY
  echo "== list (before)"; "$helper" list | head -c 2500; echo
  echo "== capture: whole system, while afplay plays a tone"
  afplay "$work/tone.wav" & tone=$!
  sleep 0.7
  "$helper" capture --system --no-stdin --seconds 3 > "$work/system.raw" 2> "$work/system.err"
  echo "exit $?"; head -c 2500 "$work/system.err"
  python3 "$here/check_tap.py" "$work/system.raw"
  echo "== list (while playing)"; "$helper" list | head -c 2500; echo
  echo "== capture: afplay only"
  "$helper" capture --path /usr/bin/afplay --app-only --no-stdin --seconds 2 > "$work/app.raw" 2> "$work/app.err"
  echo "exit $?"; head -c 2500 "$work/app.err"
  python3 "$here/check_tap.py" "$work/app.raw"
  kill $tone 2>/dev/null
  echo "== watch, while a Python process opens the microphone"
  python3 -c "
import sounddevice as sd, time
s = sd.InputStream(samplerate=16000, channels=1); s.start(); time.sleep(4); s.stop()" 2>&1 | tail -2 &
  (sleep 3) | "$helper" watch --interval 0.25 | head -c 3000
  wait
} > "$out" 2>&1
"$here/annotate.sh" "Mac audio probe" "$out"
exit 0
