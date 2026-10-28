# InputMonitor

Windows input monitoring built on a low-level keyboard hook, with optional clipboard capture. The engine is a single module (`input_monitor.py`); a Tkinter control panel (`input_monitor_ui.py`) sits on top. Standard library only, no external dependencies.

The name reflects the intended use: **input monitoring** on machines you own or administer — recording keyboard and clipboard activity for later review or export. Everything stays local; the tool makes no network connections.

## Quick start

```bat
:: open the control panel
pythonw input_monitor_ui.py

:: run headless, in the background
pythonw input_monitor.py --log C:\logs\inputs.txt
```

What the panel gives you:

- **Start / Stop** (or `F5`) — installs or removes the hook. Settings can only be changed while capture is stopped.
- **Live log** — the file is polled every 400 ms, auto-scroll can be turned off, and the view is capped at 3000 lines (the file itself grows without limit).
- **Clear** (`Ctrl+L`) — empties the log file; works while capture is running.
- **Browse / Export / Open folder** — pick the log path, copy a snapshot, reveal it in Explorer.
- **Start at logon** — writes or removes the `HKCU\...\Run` entry, then reads it back to verify.
- Top strip: state, elapsed time, keystroke count, file size. Bottom strip: pid and hook state.

Keystrokes typed into the panel itself are ignored by default ("Ignore keys typed into my own windows"), so operating the panel does not pollute the log.

## Command line

| Flag | Effect |
| --- | --- |
| `--log PATH` | Log file (default: `input-monitor.log` next to the script). |
| `--clipboard` | Also record clipboard changes (`[CLIPBOARD] ...`, truncated at 4000 chars). |
| `--no-window` | Flat log: no window/process headers and no idle separators. |
| `--include-injected` | Also record synthetic input from `SendInput` / `keybd_event`. |
| `--exclude-self` | Ignore keys typed into this process's own windows. |
| `--idle N` | Idle separator threshold in seconds (default 120). |
| `--duration N` | Exit after N seconds (useful for testing). |
| `--install` / `--uninstall` | Manage `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\<name>`. |
| `--name VALUE` | Autostart value name (default `InputMonitor`). |
| `--debug` | Print hook callback exceptions to stderr. |

Concurrency: the headless tool holds `Global\im_{…}` and the panel holds `Global\imui_{…}`. When starting capture, the panel notices an already-running background session and warns about duplicate writes; while recording it takes `Global\im_{…}` itself, so the autostart instance cannot open a second hook at the same time.

## Log format

```
===== session 2026-10-29 00:02:21  DESKTOP-X  pid=4092 clipboard=True
[00:02:27] msedge.exe :: Inbox - Google Chrome
account: samet<ENTER>
[00:02:41] [CLIPBOARD] sample clipboard text: https://example.com/login?token=abc
demo@example.com<DEL><BACK><LEFT><F1>
===== session end 2026-10-29 00:02:43 panel: stopped
```

Printable characters are written verbatim. Non-printing keys are marked (`<ENTER>`, `<TAB>`, `<BACK>`, `<DEL>`, `<LEFT>`, `<F5>`, `<NUM+>`, …), and `Ctrl`+letter pairs render as `^C`. `<ENTER>` starts a new line. Whenever the foreground window changes — and after an idle gap — a `[time] process :: title` header is written, so every line can be attributed to the application that received it.

## How it works

- **Hook** — `SetWindowsHookExW(WH_KEYBOARD_LL, …)` is installed on its own thread, which also runs the `GetMessageW` pump (the callback only fires on the thread that installed the hook). Shutdown posts `WM_QUIT` to that thread and unhooks, which is why `Engine` can be restarted.
- **Translation** — `ToUnicodeEx(vk, scan, keyState, …)` with the layout of the foreground thread. The key-state array is built from `GetAsyncKeyState` / `GetKeyState` (Shift, Ctrl, Alt, CapsLock, NumLock), because `GetKeyboardState` is unreliable inside a low-level hook. A `ToUnicodeEx` return of `-1` marks a dead key; it is stored and replayed before the next keystroke so accented pairs compose correctly.
- **Context** — on each key: `GetForegroundWindow` → `GetWindowTextW` → `QueryFullProcessImageNameW`. The header is only written when the window actually changes.
- **Clipboard** — a polling thread watches `GetClipboardSequenceNumber`, then reads `CF_UNICODETEXT` under `OpenClipboard`, retrying the common lock races.
- **Robustness** — the hook callback is fully wrapped (an escaping exception would destabilize the pump); every write is flushed, UTF-8, append-only.
- **Panel** — all Tk calls happen on the main thread; the file is read incrementally every 400 ms (`Tail`), tolerating truncation and multi-byte characters split across reads.

## Notes

- The hook is per desktop/session. Start it inside the interactive session you want to observe, as a normal user process; from a service (session 0) it sees no keystrokes.
- UIPI blocks a medium-integrity process from sending input to elevated windows, but the hook still sees the keystrokes.
- The log is plaintext and grows unbounded. Rotate it with `--duration` plus a scheduled task, or use **Clear** in the panel.
- `--install` writes only the `Run` value; nothing else is modified. Verify with `reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v InputMonitor`.
