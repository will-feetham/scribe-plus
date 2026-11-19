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
| Call transcripts: both sides of a call, labelled, ready when you hang up (Mac, test build) | | ✓ |
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
starting at sign-in, and starts it. Look for the five-bar icon in the system tray (click `^` by
the clock if it's hidden). Search Start for "Scribe Plus" to open it again; the app puts that
entry back by itself if it goes missing. Installing a newer copy updates the old one and keeps your keys and
settings. A git checkout runs where it is instead, for development.

## What you can do

| Gesture | Mac (default) | Windows (default) | What happens |
|---|---|---|---|
| Hold, talk, release | **Right Option** | **Ctrl+Win** | Dictation: transcribed, cleaned up, pasted at the cursor |
| Hold with the extra key, talk, release | **Right Option + Shift** | **Ctrl+Win+Alt** | Command Mode: rewrites the selected text ("make this friendlier", "turn this into bullets"). Press the extra key together with the hotkey; pressed later, mid-dictation, it is ignored. With nothing selected, your words are typed as a normal dictation. If the selection can't be read at all, nothing is typed, so your words can never replace text you had selected |
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

## Call transcripts (Mac, test build)

Scribe Plus can write down both sides of a phone or video call while it happens.

- **It notices the call by itself.** When a watched app (Groove OmniDialer, Zoom and Microsoft
  Teams to begin with; Google Meet in Chrome and Slack huddles can be switched on) opens the
  microphone, a recording starts. When the app lets go, it ends. "Start recording now" in the
  Calls menu does the same by hand, for anything else.
- **You can see it.** While a call is being recorded, the menu bar icon turns red and pulses.
  It goes back to normal when the recording stops.
- **Both sides, labelled.** "Me" is your microphone. "Them" is what the calling app plays, and
  only that app, so music or alerts from elsewhere stay out. Everyone on the other end is "Them".
- **Live.** Speech is transcribed a sentence at a time during the call, so the transcript is
  finished a few seconds after you hang up. The next call is recorded straight away, even while
  the last one is still being written up.
- **Where it goes.** Each call gets its own file in `Scribe Plus/Calls` in your home folder, for
  example `2026-11-19 1142 Groove OmniDialer.md`. `latest.md` in the same folder is always the
  call in progress or the last one finished (its Status line says which), so it is the one file
  to point Claude or another tool at. When a call ends the transcript is also copied to the
  clipboard; that can be switched off. A "call" in which nothing was said leaves no file.
- **On this Mac.** By default the speech-to-text runs on the Mac itself (Whisper through Apple's
  MLX, on Apple silicon), so call audio never leaves the computer. You can pick Groq or OpenAI
  for calls in the Calls menu; nothing is sent to them unless you do. Audio is not kept after it
  is transcribed. (Groq accepts 20 pieces of speech a minute. In a fast back-and-forth the cloud
  transcript can fall behind and finish a little after the call; on the Mac there is no limit.)
- **If something goes wrong, it says so.** If the part that records one side stops, it is started
  again, and the transcript carries a note of when. If a side could not be recorded at all, the
  notification and the transcript say which, and why.

Set it up by adding `--calls` to the install line (`... | bash -s -- --calls`). That installs
the on-device speech model, a 1.5 GB download the first time. It needs macOS 14.2 or newer, and
macOS asks for one more permission on the first call: System Audio Recording, which is what lets
Scribe Plus hear the other side. Headphones give the cleanest result. On speakers your microphone
also hears the other side; repeated lines are dropped, but not perfectly.

If your calls ring through to a desk phone or mobile (Groove's Bridge Dial, for example), the Mac
never hears them and there is nothing to record. And recording calls is your responsibility: make
sure the people on the call know, and that your employer allows it.

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
Call transcripts are separate: they are made on the Mac itself unless you choose a cloud provider
for them, and their audio is not kept.
API keys live only in the app folder's `.env`, which git ignores. Audio clips, history and logs
stay in the data folder above. There is no analytics or phone-home, same as Scribe. The menu's
"get updates" item just opens the download page.

## Troubleshooting

**Both**

- **"Access denied. Please check your network settings." while on a VPN.** Groq refuses many VPN
  and hosting addresses. The fix that works: in your VPN's split tunneling settings, exclude the
  website **`api.groq.com`**, then reconnect the VPN. In Surfshark that's Settings > VPN settings >
  Bypasser > Websites / IP addresses: turn on Bypass VPN and add `api.groq.com`. Only the requests
  to Groq skip the VPN; everything else stays on it. Excluding the app instead doesn't work
  reliably on Windows: Scribe Plus runs inside Python, and the program that actually talks to Groq
  isn't the one in the Scribe Plus folder. Turning the VPN off also works, and OpenAI and Anthropic,
  which you can pick in the menu, have accepted VPN addresses that Groq refused.

**Mac**

- **A call was not recorded, or "Them" is missing.** Menu bar icon > Permissions > System Audio
  Recording, and turn on Scribe Plus. Check that the app is ticked under Calls > In these apps.
  The log (`~/.config/scribe/plus/scribe-plus.log`) has a "call watch" line each time a watched
  app starts or stops using the microphone, which shows whether the call was noticed.
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
- **Uninstall:** quit Scribe Plus (it puts its Start menu entry back when it starts), then delete
  `%LOCALAPPDATA%\Programs\Scribe Plus`, the "Scribe Plus" shortcuts in the Start menu and Startup
  folder, the registry key `HKCU\Software\Classes\AppUserModelId\Feetham.ScribePlus` (its name
  for notifications), and `%APPDATA%\scribe\plus` if you want your settings gone.

## How it's put together

```
scribe_plus_windows.py   tray app (Windows)
scribe_plus_mac.py       menu bar app (macOS); --smoke-test for CI
benchmark.py             engine comparison on your own clips
scribe_plus/
  tray.py                the app and its menu, shared by both platforms
  hotkeys.py             gesture state machine (pure Python, unit-tested)
  pipeline.py            record > prep > speech > cleanup/command > paste > log
  engines.py             Groq, OpenAI, Anthropic, Ollama, local Whisper, MLX Whisper; keep-alive + prewarm
  calls/                 call transcripts: detection, utterance cutting, live transcript, files
  textproc.py            prompts, guard, dictionary, snippets, fallback cleanup
  pill_model.py          the status pill's look and animation, drawn by both platforms
  audio.py, icons.py, metrics.py, settings.py
  win/                   keyboard hook, clipboard, foreground app, pill (tkinter), dialogs
  mac/                   event tap, pasteboard, frontmost app, pill (AppKit), permissions, calls
macos/                   the app bundle's launcher, Info.plist and icon; audiotap.m (call helper)
install-mac.sh, install-windows.ps1, Install Scribe Plus.bat
site/                    scribeplus.feetham.us (GitHub Pages)
.github/workflows/       tests on Linux, Windows and macOS; releases; the site
tests/                   run with: python -m pytest
```

**Releases are automatic.** Every push to `main` runs the tests on all three systems, including
a real install and smoke test of the Mac app on GitHub's Mac machines. If the version in
`scribe_plus/__init__.py` hasn't been released yet, GitHub then builds the Windows zip and the
Mac package and publishes the release. The download page always points at the newest one.

**Test builds** come from the `beta` branch. A version like `1.1.0b1` there is published as a
GitHub pre-release with its own install line; the download page and the normal install line keep
pointing at the last full release until the version lands on `main`. A test build's install line
always installs that exact build; the install line on the download page goes back to the release.

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
