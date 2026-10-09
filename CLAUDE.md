# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Busyist is a Windows-only tray app that runs Todoist pomodoros on a BUSY Bar (a hardware timer), or on the PC when there is no bar. Python backend (`busyist.py`) + pywebview/WebView2 UI (`ui/`). There is no test suite and no linter configured.

## Commands

```powershell
# Run from source (Python 3.11+, Windows). --show opens the window instead of starting in the tray.
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe busyist.py --show      # or run.bat (pythonw, no console)

# Build installer + portable zip + SHA256SUMS into dist\ (needs Inno Setup 6; -SkipInstaller to skip it)
powershell -ExecutionPolicy Bypass -File build.ps1
```

- Logs go to `%APPDATA%\Busyist\busyist.log` (only a console when run with `python`, not `pythonw`).
- Release: bump `__version__` in `busyist.py`, commit, push a matching tag `vX.Y.Z`. CI (`.github/workflows/release.yml`) fails if the tag and `__version__` differ; `build.ps1`, the PyInstaller spec and the Inno script all read the version from that one line by regex, so keep its `__version__ = "x.y.z"` format.
- The in-app updater depends on the release assets: it looks for `Busyist-Setup-<version>.exe` and `SHA256SUMS.txt` on the GitHub "latest release", so keep those names. It runs the new Setup with `/VERYSILENT /RELAUNCH=1`, and `DeinitializeSetup` in `packaging/busyist.iss` starts the app again afterwards.
- `build.ps1` aborts if `config.json`/`session.json`/`history.json` end up in `dist\`. Those files are gitignored personal data (tokens, PIN); never commit them.

## Architecture

All backend logic is one file, `busyist.py`, in four layers:

- **`Todoist`**: thin REST client (`api/v1`, paginated) for tasks by filter query, labels, comments, close.
- **`Bar`**: the BUSY Bar over Wi-Fi and/or USB (`10.0.4.20`) via `busylib`. Every call goes through `Bar.run`, which holds one RLock and tries the last-working route first. The bar has no "start timer" command: the app writes a whole *snapshot* (`BusySnapshotInterval` / `NotStarted`) and the bar ignores any snapshot whose timestamp isn't newer than its own, hence `Bar._write` using `max(now, live+1)`. Pause/stop/skip are read-modify-write snapshot edits (busylib's helpers are async-only, so they're reimplemented synchronously).
  - **No bar**: with `use_busybar` off (the default for new installs; configs saved before the setting existed are migrated to `True` in `App.__init__`), `Bar.run` hands every call to a `LocalClock` instead of a `BusyBar` client. `LocalClock` implements only `busy_snapshot`, `busy_snapshot_set` and `busy_profile`, and keeps the last snapshot in `timer.json`; `timer_state` does the arithmetic, so the same start/pause/skip/stop code and the engine work unchanged. A new bar call used inside `Bar` needs a `LocalClock` counterpart. Switching modes while a session runs is refused (the session's stamp belongs to the old source). `state()["bar"]["local"]` tells the UI which mode it is in.
- **`App`**: owns state, guarded by `App.lock`. Lock order is `bar.lock` then `app.lock` (see `start_task`). The **engine thread** polls the bar's snapshot every `poll_seconds` (every 1s in no-bar mode), then `follow_session()` derives session progress *from the bar's state*, not from a local timer: even interval indices are work, odd are rest; "finished"/"stopped" are inferred from the snapshot, and snapshots older than `session["stamp"]` are ignored. When a session ends, `finish()` runs on its own thread (Todoist comment, optional label removal, history append, "complete task" prompt). The running session is persisted to `session.json` so a restart resumes it (`self.recovering` makes that end silently).
- **`Api`**: the object exposed to JS as `window.pywebview.api.*`. Every method goes through `Api._do`, which returns `{ok, ...}` / `{ok: False, error}` and turns `AppError` into a user-facing message. Raise `AppError` for expected failures.

**Updates** (`App.updater`, packaged builds only): every 6 h it asks GitHub for the latest release. A Setup-installed copy (`unins000.exe` next to the exe) that the user can write to downloads the installer, checks it against `SHA256SUMS.txt` and installs it once nothing is going on (no session, no end-of-session summary, window closed). It then quits so Setup can replace its files. All-users installs don't install on their own: they notify, and the Install button goes through an admin prompt. Portable copies only link to the release. `update.json` in the data folder records the target version so the next start can report whether the update worked.

**Site blocking** (issue #2, `FocusServer`): a `http.server.ThreadingHTTPServer` on `127.0.0.1:47616` (next to the single-instance socket on 47615) serves `GET /focus` → `App.site_focus()` (`{active, task, ends_at_ms, sites}`, the same work-phase rule as the app blocker but gated on `block_sites`). It answers only requests whose `Origin` is our pinned `chrome-extension://<EXTENSION_ID>` and 403s everything else, so a web page can't read the current task. The Chrome MV3 extension in `extension/` (manifest `key` pins the id) polls it and uses `declarativeNetRequest` to redirect `blocked_sites` to its `blocked.html`. `EXTENSION_SRC` (`extension/`) ships via `datas` in the spec; the packaged app copies it at startup (`sync_extension`) to `EXTENSION_DIR` = `%APPDATA%\Busyist\chrome-extension`, a stable path that survives updates (from source, `EXTENSION_DIR` is the repo folder). `Api.install_extension` copies that path to the clipboard, selects it in Explorer and opens `chrome://extensions` (Edge as fallback, `find_browser`) for "Load unpacked". `blocked_sites` are bare domains validated by `clean_blocked_sites`/`clean_domain`.

Also in `busyist.py`: pystray tray icon (drawn with Pillow, shows minutes left), `GlobalHotkey` (Win32 `RegisterHotKey` on its own thread; hotkey cycles window → mini → tray), launch-at-login via the registry Run key, and single-instance enforcement (a second launch connects to `127.0.0.1:47615`, which makes the first instance show its window).

### UI

`ui/main.html|js` (main window: task list, saved filters, settings) and `ui/mini.html|js` (frameless always-on-top timer) are plain JS with no build step or framework. Both are separate pywebview windows sharing one `Api`. The pages **poll `get_state()`** (mini: every 1s, interpolating the countdown locally from `state.now`); Python can also push with `window.evaluate_js` (e.g. `window.Mini.refresh`), but failures there are swallowed because the page may not be loaded yet. `Api.get_state` / `App.state()` is the single source of truth for what the UI shows.

### Data and packaging

- Runtime data lives in `%APPDATA%\Busyist` (`config.json`, `session.json`, `history.json`, `timer.json`, log, `webview/`), never next to the program. `DEFAULTS` in `busyist.py` is the full config schema; `adopt_old_files()` migrates files from the script directory when running from source.
- Resources are found via `RES_DIR` (`sys._MEIPASS` when frozen). New files the app needs at runtime (assets under `ui/` are already covered) must be added to `datas` in `packaging/busyist.spec`; new hidden imports go there too (pywebview/pystray/busylib are loaded dynamically).
- Dependencies are pinned in `requirements.txt` (runtime) and `requirements-build.txt` (PyInstaller); `build.ps1` uses a separate `.venv-build`. The repo's `old/`, `.webview/`, `.venv*` are local and gitignored.
