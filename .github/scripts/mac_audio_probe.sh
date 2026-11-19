#!/usr/bin/env bash
# Exercises macos/audiotap on a GitHub Mac runner: records a test tone from
# the whole system and from one app, and checks that an app opening the
# microphone is noticed. Results appear as notices on the run.
#
# Runners can't answer permission prompts, so the System Audio Recording
# permission is granted the way the runner image grants Microphone: a row
# in the privacy database. If a future image stops allowing that, this
# reports it rather than failing the build (the step is continue-on-error).
set +e
helper="${1:-dist/audiotap}"
work="${RUNNER_TEMP:-/tmp}/audio-probe"
mkdir -p "$work"
here="$(cd "$(dirname "$0")" && pwd)"
fails=0
note() { "$here/annotate.sh" "$1" "$2"; }
procs() {   # one line per process that matches $1, from "audiotap list"
  "$helper" list | python3 -c "
import json, sys
want = sys.argv[1]
for p in json.load(sys.stdin)['procs']:
    if want in p['path'] or want in p['bundle']:
        print(p['pid'], p['bundle'] or '-', p['path'].rsplit('/', 1)[-1], 'input' if p['input'] else '', 'output' if p['output'] else '')
" "$1"
}

{
  sw_vers | tr '\n' ' '; uname -m
  "$helper" version
  for db in "$HOME/Library/Application Support/com.apple.TCC/TCC.db" \
            "/Library/Application Support/com.apple.TCC/TCC.db"; do
    sudo sqlite3 "$db" "CREATE TEMP TABLE t AS SELECT * FROM access WHERE service='kTCCServiceMicrophone';
      UPDATE t SET service='kTCCServiceAudioCapture';
      INSERT OR REPLACE INTO access SELECT * FROM t;
      SELECT count(*) || ' permission rows' FROM access WHERE service='kTCCServiceAudioCapture';" 2>&1 | head -3
  done
  system_profiler SPAudioDataType 2>&1 | grep -E "Device:|Default|SampleRate" | head -12
} > "$work/setup.txt" 2>&1
note "Call helper: runner" "$work/setup.txt"

python3 - "$work/tone.wav" <<'PY'
import math, struct, sys, wave
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 44100)))
                           for i in range(44100 * 12)))
PY

{
  echo "-- whole system, while afplay plays a tone"
  afplay "$work/tone.wav" & tone=$!
  sleep 0.7
  "$helper" capture --system --no-stdin --seconds 3 > "$work/system.raw" 2> "$work/system.err"
  echo "exit $?"; head -c 900 "$work/system.err"
  python3 "$here/check_tap.py" "$work/system.raw" --need-sound || fails=$((fails + 1))
  echo "-- afplay only"
  procs afplay
  "$helper" capture --path /usr/bin/afplay --app-only --no-stdin --seconds 3 > "$work/app.raw" 2> "$work/app.err"
  echo "exit $?"; head -c 900 "$work/app.err"
  python3 "$here/check_tap.py" "$work/app.raw" --need-sound || fails=$((fails + 1))
  echo "-- an app that is not playing anything (expect silence)"
  "$helper" capture --match com.apple.controlcenter --app-only --no-stdin --seconds 2 > "$work/other.raw" 2> "$work/other.err"
  echo "exit $?"; head -c 300 "$work/other.err"
  python3 "$here/check_tap.py" "$work/other.raw"
  kill $tone 2>/dev/null
  echo "failures so far: $fails"
} > "$work/capture.txt" 2>&1
note "Call helper: capture" "$work/capture.txt"

{
  echo "-- watch, while a Python process opens the microphone for 3 s"
  ( sleep 1; python3 -c "
import sounddevice as sd, time
s = sd.InputStream(samplerate=16000, channels=1); s.start(); time.sleep(3); s.stop(); s.close()" 2>&1 | tail -2 ) &
  (sleep 6) | "$helper" watch --interval 0.25 > "$work/watch.jsonl"
  wait
  python3 - "$work/watch.jsonl" <<'PY' || fails=$((fails + 1))
import json, sys
seen_on = seen_off = False
for line in open(sys.argv[1]):
    ev = json.loads(line)
    mic = [p for p in ev["procs"] if p["input"]]
    print(round(ev["t"] % 100, 2), "mic:", [p["path"].rsplit("/", 1)[-1] for p in mic],
          "playing:", [p["path"].rsplit("/", 1)[-1] for p in ev["procs"] if p["output"]])
    if any("ython" in p["path"] for p in mic):
        seen_on = True
    elif seen_on:
        seen_off = True
print("microphone use noticed:", seen_on, "and its end:", seen_off)
sys.exit(0 if seen_on and seen_off else 1)
PY
  echo "failures: $fails"
} > "$work/watch.txt" 2>&1
note "Call helper: watch" "$work/watch.txt"
exit $fails
