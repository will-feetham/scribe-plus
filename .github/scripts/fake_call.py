"""A stand-in for a calling app, for the CI call test.

    python fake_call.py speech.wav

Opens the microphone (that is what a call looks like to Scribe Plus), waits
for the "call" to connect, plays a spoken sentence as the other side, then
hangs up.
"""
import sys
import time
import wave

import numpy as np
import sounddevice as sd

with wave.open(sys.argv[1], "rb") as w:
    rate = w.getframerate()
    channels = w.getnchannels()
    data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
if channels > 1:
    data = data.reshape(-1, channels).mean(axis=1)

mic = sd.InputStream(samplerate=16000, channels=1)
mic.start()
print("fake call: microphone open", flush=True)
time.sleep(5.0)
sd.play(data, rate)
sd.wait()
print("fake call: finished speaking", flush=True)
time.sleep(2.0)
mic.stop()
mic.close()
print("fake call: hung up", flush=True)
