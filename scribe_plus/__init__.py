"""
Scribe Plus: an AI dictation layer on top of Scribe by Avi Ibrahms (github.com/avibrahms/scribe).

Scribe (upstream) records audio and pastes a raw Whisper transcript.
This package adds:

  * an AI cleanup pass (fillers, self-corrections, formatting)
  * Command Mode (rewrite selected text by voice)
  * a personal dictionary and voice snippets
  * app-aware style (casual in Slack, polished in Gmail, literal in code)
  * hands-free mode, a floating status pill, and a latency/cost log

Platform-neutral modules live here; Windows glue lives in scribe_plus.win
and Mac glue in scribe_plus.mac.
Scribe's own files (scribe_core.py, scribe_windows.py, scribe.py) are kept
as they are, apart from one comment, so `git pull upstream main` merges
Scribe's fixes cleanly.
"""

__version__ = "1.0.1"
