"""
Call transcripts: both sides of a phone or video call, labelled Me and Them.

    mic  ---------------> "Me"   \\
                                   segmenter -> speech-to-text -> one transcript
    the calling app's sound -> "Them" /                            per call (live)

A call starts when a watched app (Groove OmniDialer, Zoom, Teams, ...) opens
the microphone and ends when it lets go, or by hand from the menu. Speech is
cut into utterances and transcribed while the call is still going, so the
transcript is complete a few seconds after hang-up.

  stream.py      the audio packet format shared with the capture helpers,
                 and resampling to 16 kHz
  segmenter.py   cuts a live audio stream into utterances
  transcript.py  the transcript, its Markdown file and the "latest" copy
  recorder.py    one call: two channels in, transcript out
  detector.py    decides when a call starts and ends
  manager.py     ties detection, capture and the recorder together
  micproc.py     microphone capture, run as a child process
"""
