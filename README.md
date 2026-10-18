<img src="assets/icon.png" width="72" alt="Scribe Plus icon" align="right">

# Scribe Plus

**AI dictation for Mac and Windows, built on [Scribe](https://github.com/avibrahms/scribe) by Avi Ibrahms.**

Hold a hotkey, talk, let go. Your words are transcribed, cleaned up and pasted wherever your
cursor is, in any app.

**Download: [scribeplus.feetham.us](https://scribeplus.feetham.us)** (always the latest release).

> **Credit where it's due.** Scribe Plus is a fork of [Scribe](https://github.com/avibrahms/scribe),
> Avi Ibrahms' free, open-source push-to-talk dictation app for macOS and Windows (MIT). Scribe
> does the hard groundwork: recording, fast transcription through Groq, the paste that keeps your
> clipboard, history, and a filter for the phantom phrases speech models invent in silence.
> Scribe Plus reuses all of that and adds an AI layer on top. If you want read-aloud
> (text-to-speech), use [Scribe](https://github.com/avibrahms/scribe) itself.

## What Scribe Plus adds to Scribe

| | Scribe | Scribe Plus |
|---|---|---|
| Push-to-talk dictation, auto-paste, history | ✓ | ✓ (Scribe's own code) |
| AI cleanup: fillers, false starts, "no wait" corrections, lists, numbers | | ✓ |
| Command Mode: rewrite selected text by voice | | ✓ |
| Personal dictionary and voice snippets | | ✓ |
| Tone that fits the app you're in (Slack, Gmail, VS Code...) | | ✓ |
| Hands-free mode (double-tap, no holding) | | ✓ |
| Animated status pill above the Dock or taskbar | | ✓ |
| Latency and cost log, plus a benchmark across engines | | ✓ |
| Read-aloud (text-to-speech) | ✓ | |
| macOS | ✓ | ✓ |
| Windows | ✓ | ✓ |

Every install needs a free Groq API key ([console.groq.com/keys](https://console.groq.com/keys));
the installer asks for it.

## Install on a Mac (macOS 12 or newer)

Open Terminal, paste this line and press Return:

```
curl -fsSL https://scribeplus.feetham.us/install.sh | bash
```

It installs everything inside your own account (no admin password): a private Python set up with
[uv](https://docs.astral.sh/uv/) in `~/Library/Application Support/Scribe Plus`, and
**Scribe Plus.app** in `~/Applications`. It asks whether to start Scribe Plus when you log in, then
starts it. macOS asks you to turn on Scribe Plus under **Input Monitoring** (to see the hotkey),
**Accessibility** (to paste) and **Microphone**; Scribe Plus restarts itself once they're on. Run
the same line again to update. Add `--local-whisper` (`... | bash -s -- --local-whisper`) for
offline speech-to-text.

Nothing is downloaded through a web browser, so macOS doesn't block it as an app from an
unidentified developer. The app bundle never changes between releases (the code lives next to it),
so the permissions you grant stay granted after updates.

## Install on Windows (10 or 11)

[Download the zip](https://github.com/will-feetham/scribe-plus/releases/latest/download/ScribePlus-Windows.zip),
unzip it and double-click **Install Scribe Plus.bat**, or paste this into PowerShell:

```
irm https://scribeplus.feetham.us/install.ps1 | iex
```

The installer finds Python 3.10 to 3.13 (and offers to install 3.12 if it's missing), installs
Scribe Plus into `%LOCALAPPDATA%\Programs\Scribe Plus`, adds it to the Start menu, asks about
starting at sign-in, and starts it. Look for the five-bar icon in the system tray (click `^` by the
clock if it's hidden). Installing a newer copy updates the old one and keeps your keys and
settings. A git checkout runs where it is instead, for development.

## What you can do

| Gesture | Mac (default) | Windows (default) | What happens |
|---|---|---|---|
| Hold, talk, release | **Right Option** | **Ctrl+Win** | Dictation: transcribed, cleaned up, pasted at the cursor |
| Hold with the extra key, talk, release | **Right Option + Shift** | **Ctrl+Win+Alt** | Command Mode: rewrites the selected text ("make this friendlier", "turn this into bullets"). With nothing selected, it writes what you ask for |
| Double-tap | **Right Option** | **Ctrl+Win** | Hands-free: keeps listening; press the hotkey again to finish |
| While recording | **Esc** | **Esc** | Cancel (the Esc is swallowed so it doesn't close your dialog) |
| Any time | **Control+Command+V** | **Ctrl+Alt+V** | Paste the last dictation again |

The hotkey plus any other key is ignored, so your normal shortcuts still work. Other hotkeys are in
the menu (Mac: Control+Option, Right Command, Fn / Globe and more; Windows: Right Alt, Right Ctrl,
Right Shift and more).

The cleanup pass removes fillers and false starts, applies self-corrections ("Tuesday, no wait,
Wednesday" becomes "Wednesday"), turns spoken lists into lists, and formats numbers, emails and
links. It is told never to answer or act on what you say, and a guard falls back to a plain
cleanup if the model's output looks like an answer or a rewrite.

**App-aware style** picks a tone from the window you're in: casual for Slack, Teams and Discord;
full paragraphs for Gmail and Outlook; literal for VS Code, Cursor and terminals; plain requests
for chat assistants. The rules live in `settings.json` (`app_rules`, `styles`). Changes made from
the tray menu apply instantly; hand edits to `settings.json` apply on the next dictation, except
the hotkey, hands-free and Command Mode switches, which apply after a restart.

**Dictionary** (`dictionary.txt`): put names and jargon one per line so they're spelled right,
or `heard -> written` to fix a word the speech model keeps getting wrong.

**Snippets** (`snippets.txt`): `insert my calendar link = https://cal.com/you`. Say the trigger
phrase and the saved text goes in instead.

Both files, `settings.json` and the logs live in `%APPDATA%\scribe\plus` on Windows and
`~/.config/scribe/plus` on a Mac. The menu opens each of them.

## Engines and what they cost

| Stage | Default | Alternatives in the menu |
|---|---|---|
| Speech-to-text | Groq `whisper-large-v3-turbo` | Groq `whisper-large-v3`, OpenAI `gpt-4o-mini-transcribe`, local Whisper (offline) |
| Cleanup | Groq `openai/gpt-oss-20b` | Groq `openai/gpt-oss-120b`, Claude Haiku 5.5, a local model via Ollama, Off |
| Command Mode | Groq `openai/gpt-oss-120b` | follows the cleanup engine |

Why these defaults:

- Speed decides whether dictation feels natural. About 1 second is the limit for your flow of
  thought to stay uninterrupted, and 0.1 second feels instant
  ([Nielsen Norman Group](https://www.nngroup.com/articles/response-times-3-important-limits/)).
  So the defaults are the fastest cloud models available, and the latency log measures the time
  from releasing the key to text on screen for every dictation.
- Groq Whisper Large v3 Turbo runs at about 216x real time for $0.04 per audio hour, billed at a
  10-second minimum per request ([Groq](https://groq.com/whisper-large-v3-turbo-now-available-on-groq-combining-speed-quality-for-speech-recognition/),
  [pricing](https://groq.com/pricing)), so about $0.0001 per dictation.
- Groq retired its Llama models for free and developer accounts on 2026-08-16 and points to
  `openai/gpt-oss-20b` as the replacement ([Groq deprecations](https://console.groq.com/docs/deprecations)).
  Groq lists gpt-oss-20b at about 1,000 tokens per second, $0.075 in and $0.30 out per million
  tokens ([Groq models](https://console.groq.com/docs/models)). Scribe Plus asks it for low
  reasoning effort to keep latency down.
- Free tier limits (third-party reading, August 2026): gpt-oss models get 30 requests per minute,
  1,000 per day, 8,000 tokens per minute
  ([ampm-aiops](https://ampm-aiops.com/en/guides/groq-free-tier-llama-removed-gpt-oss-2026/)).
  A cleanup call is roughly 700 tokens, so heavy days may need Groq's pay-as-you-go tier.
- Local Whisper is free and private but runs on the CPU, so expect seconds per dictation, not a
  fraction of one. Install it with **Install local Whisper.bat** on Windows, or on a Mac by
  running the install line with `--local-whisper`.

The menu's **Check setup** item confirms your keys work and that the configured models are still
served on your account, since providers retire models.

## Research tools

**Latency log** (`metrics.csv`, menu > Open latency log): one row per dictation with the time
for each stage (mic open, stop, audio prep, speech, cleanup, paste), the total from key release
to text on screen, token counts, an estimated cost, the guard result, the raw transcript and the
final text. Window titles are not logged unless you set `log_window_titles` to true.

**Benchmark** (menu > Run benchmark, or **Benchmark.bat** on Windows): replays your newest saved clips
through every speech and cleanup engine you have keys for, then writes a CSV and an HTML report
with median and 90th-percentile latency, cost per 1,000 dictations, speech disagreement versus
Whisper Large v3, and guard rejections. It also summarizes your real dictations from the latency
log. Clips are saved only while "Save audio for benchmarks" is on (the newest 300 are kept).

## What leaves your computer

Audio goes to the speech provider you pick (Groq by default), and the transcript goes to the
cleanup provider (Groq by default). With local Whisper and Ollama, nothing leaves the machine.
API keys live only in the app folder's `.env`, which git ignores. Audio clips, history and logs
stay in the data folder above. There is no analytics or phone-home, same as Scribe. The menu's
"get updates" item just opens the download page.

## Troubleshooting

**Mac**

- **The hotkey does nothing.** Open the menu bar icon > Permissions, turn on Scribe Plus under
  Input Monitoring and Accessibility in System Settings, then choose Restart Scribe Plus. If it
  was already on, turn it off and on again.
- **The log** is `~/.config/scribe/plus/scribe-plus.log`. To watch it live, quit Scribe Plus and
  run `"$HOME/Applications/Scribe Plus.app/Contents/MacOS/Scribe Plus" --debug` in Terminal.
- **Right Option types special characters.** Only while you hold it with another key; holding it
  alone records. Pick another hotkey in the menu if you use Right Option for accents.
- **Uninstall:** quit Scribe Plus, then run
  `launchctl bootout gui/$(id -u)/us.feetham.scribeplus; rm -rf ~/Applications/"Scribe Plus.app" ~/Library/Application\ Support/"Scribe Plus" ~/Library/LaunchAgents/us.feetham.scribeplus.plist`
  (your settings and history stay in `~/.config/scribe/plus` until you delete it).

**Windows**

- **Run "Scribe Plus (debug).bat"** to watch the log live (quit the tray copy first). The log is
  also in `%APPDATA%\scribe\plus\scribe-plus.log`.
- **The Start menu pops up after Ctrl+Win.** Scribe Plus taps an unassigned key while Win is held
  to stop this (the same trick AutoHotkey uses). If it still happens, pick another hotkey in the
  tray menu; Right Ctrl is the simplest.
- **Nothing pastes into an app running as administrator.** Windows blocks a normal app from
  typing into an elevated one. Run that app normally, or run Scribe Plus as administrator.
- **Antivirus warns about pynput.** As with Scribe, the hotkey uses a keyboard hook that some
  antivirus tools flag. It only watches for the hotkey locally.
- **No audio.** Settings > Privacy & security > Microphone > allow desktop apps. Pick a specific
  mic under tray > Microphone.
- **VS Code and Command Mode.** With nothing selected, VS Code copies the whole line on Ctrl+C,
  so select the text you want rewritten first.
- **Clipboard.** As in Scribe on Windows, only text survives the paste round trip; an image you
  had copied is replaced by the dictated text. (On a Mac everything you had copied is put back.)
- **Uninstall:** delete `%LOCALAPPDATA%\Programs\Scribe Plus`, the "Scribe Plus" shortcuts in
  the Start menu and Startup folder, and `%APPDATA%\scribe\plus` if you want your settings gone.

## How it's put together

```
scribe_plus_windows.py   tray app (Windows)
scribe_plus_mac.py       menu bar app (macOS); --smoke-test for CI
benchmark.py             engine comparison on your own clips
scribe_plus/
  tray.py                the app and its menu, shared by both platforms
  hotkeys.py             gesture state machine (pure Python, unit-tested)
  pipeline.py            record > prep > speech > cleanup/command > paste > log
  engines.py             Groq, OpenAI, Anthropic, Ollama, local Whisper; keep-alive + prewarm
  textproc.py            prompts, guard, dictionary, snippets, fallback cleanup
  pill_model.py          the status pill's look and animation, drawn by both platforms
  audio.py, icons.py, metrics.py, settings.py
  win/                   keyboard hook, clipboard, foreground app, pill (tkinter), dialogs
  mac/                   event tap, pasteboard, frontmost app, pill (AppKit), permissions
macos/                   the app bundle's launcher, Info.plist and icon
install-mac.sh, install-windows.ps1, Install Scribe Plus.bat
site/                    scribeplus.feetham.us (GitHub Pages)
.github/workflows/       tests on Linux, Windows and macOS; releases; the site
tests/                   run with: python -m pytest
```

**Releases are automatic.** Every push to `main` runs the tests on all three systems, including
a real install and smoke test of the Mac app on GitHub's Mac machines. If the version in
`scribe_plus/__init__.py` hasn't been released yet, GitHub then builds the Windows zip and the
Mac package and publishes the release. The download page always points at the newest one.

Scribe's own files are here too: `scribe_core.py` (shared engine), `scribe_windows.py` and
`scribe.py` (Scribe's Windows and macOS apps), `install.bat`, `install.sh` and the `bin/`
read-aloud helpers. Scribe Plus imports Scribe's recorder, history and hallucination filter from
`scribe_core.py` and leaves those files as they are, so Scribe's fixes merge cleanly:

```
git remote add upstream https://github.com/avibrahms/scribe   (once)
git pull upstream main
```

To work on Scribe Plus, install the test tools with
`venv\Scripts\python.exe -m pip install -r requirements-dev.txt`, then run the tests as above.

## License

MIT, see [LICENSE](LICENSE). Scribe is copyright Avi Ibrahms; the Scribe Plus additions are
copyright Will Feetham, under the same license.

## Thanks

- [Scribe](https://github.com/avibrahms/scribe) by Avi Ibrahms, the app this is built on.
- [Groq](https://groq.com/) for fast Whisper and gpt-oss inference.
- [pystray](https://github.com/moses-palmer/pystray) and [pynput](https://github.com/moses-palmer/pynput)
  for the tray icon and keyboard hook.
