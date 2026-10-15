# Idea: link tasks to Herdr panes

Status: **parked**. Researched, nothing built yet. Research based on herdrdev/herdr at commit `212b710` (2026-10-12).

## The idea

Link a buZyist task to a [Herdr](https://github.com/herdrdev/herdr) pane, where a coding agent runs.

- While the agent in the pane is **working**, buZyist pauses the pomodoro.
- When the agent goes back to **idle**, or needs a manual action (**blocked**), buZyist reminds the user.
- The user hits **Play** to resume or restart the focus session. Nothing resumes on its own.

## What Herdr is

Herdr is a terminal workspace manager for coding agents such as Claude Code, Codex, Cursor, OpenCode and Pi. It is a single Rust binary, with a background server plus clients. It organises work into workspaces, tabs and panes, and it supports Windows.

Docs: <https://herdr.dev/docs/>, in particular:

- [concepts](https://herdr.dev/docs/concepts/)
- [socket API](https://herdr.dev/docs/socket-api/)
- [CLI reference](https://herdr.dev/docs/cli-reference/)
- [session state](https://herdr.dev/docs/session-state/)

### Agent states, and what buZyist would do

| Herdr state | Meaning | buZyist would… |
| --- | --- | --- |
| `working` | agent is running | pause the pomodoro |
| `blocked` | needs input, approval or a decision | remind: "needs you" |
| `done` | finished, not looked at yet | remind: "finished" |
| `idle` | finished or waiting, already seen | softer reminder, or nothing |
| `unknown` | Herdr can't tell | do nothing |

The API treats `idle` and `done` alike: both mean the agent is ready for input.

### Ways to connect

- **Socket API:** newline-delimited JSON. On Windows it is a named pipe: `\\.\pipe\<socket path>` (see `src/platform/windows.rs`). The default socket lives under the Herdr config directory. The path is resolved in this order: `--session`, then `HERDR_SOCKET_PATH`, then `HERDR_SESSION`, then the default. A Python client can open the pipe with the standard library.
  - Request: `{"id":"req_1","method":"ping","params":{}}`
  - Response: `{"id":"req_1","result":{...}}`, or `{"id":..., "error":{"code","message"}}`.
- **Live updates:** `events.subscribe` with `{"type":"pane.agent_status_changed","pane_id":"w1:p1"}`. The `pane_id` and `agent_status` filters are optional. The connection stays open and events are pushed, so no polling is needed.
- **Pane picker:** `agent.list`, `pane.list`, `pane.get`, and `session.snapshot` (CLI: `herdr api snapshot`).
- **Extras:**
  - `agent.focus` / `pane.focus`: a "Go to pane" button on the reminder.
  - `notification.show`: also show the reminder inside Herdr.
- **CLI fallback:**
  - `herdr agent list`
  - `herdr pane get <id>`
  - `herdr agent wait <pane> --until blocked|done`
- **Plugins:** Herdr plugins have event hooks that run a command on an event. That's an alternative push path, for example POSTing to buZyist's local server. It's probably not needed, since the subscription covers it.

## Planned design

1. **`buzyist/herdr.py`:** a small pipe client, around 150 lines: `call(method, params)`, `list_agents()`, and a `subscribe()` generator. Standard library only.
2. **`buzyist/app_herdr.py` (`HerdrMixin`):** a thread that keeps the subscription open. It maps states onto the existing `control("pause")` and `notify()`. Pausing is a snapshot edit, so it works the same on the bar and with `LocalClock`; the engine, `follow_session` and the blockers need no changes. The blockers relax during a pause because `App.focus()` only blocks during a work phase.
3. **Config:**
   - Settings: `herdr_on`, a `task_id → pane` link map, and which states trigger a reminder.
   - Every key needs a `DEFAULTS` entry and a `Config` field; add a `MIGRATIONS` entry only if the shape of an existing setting changes.
   - Copy the link into `session` at start so it survives restarts through `session.json`.
4. **UI:**
   - a "Link Herdr pane" picker per task
   - a status chip showing the agent's state, via `state()["herdr"]`
   - a reminder toast with **Play** and **Go to pane**
   - an optional sound or mini-window flash
5. **Tests:** a fake pipe or NDJSON server, plus the existing `clock`, `app` and `deferred_threads` fixtures. If the CLI fallback is used, its `subprocess` calls are recorded by `side_effects`.

Rough size: about 300–500 lines of Python, about 150 lines of JS/HTML, plus tests. **About 1.5–3 days.**

## Considerations / pitfalls

- **Lost events:** if buZyist falls behind, Herdr sends `error.code: "events_lost"` and closes the subscription. Resubscribe first, then re-read the state with `agent.list` or `session.snapshot`. Don't replay buffered events over a snapshot.
- **Missing panes:** subscribing to a pane that doesn't exist rejects the whole request and closes the connection. Check `pane.list` first, and subscribe without a `pane_id` filter if needed (filter on our side).
- **Pane ids may change:** ids like `w1:p1` may not survive a Herdr server restart. Herdr restores the layout, but the docs don't say ids stay the same. Store each link's pane label, cwd and agent too, so it can be found again; also handle `pane.moved`/`pane.closed`.
- **Who paused:** only auto-pause, and only remind, for pauses Herdr caused. Track `paused_by: "herdr" | "user"`. A manual pause is never touched, and nothing auto-resumes.
- **Debounce:** agents can switch between `working` and `blocked` quickly, so wait a few seconds before pausing or reminding.
- **Breaks:** do nothing during a break phase, or decide what should happen.
- **Remote sessions** (`sync_session`): never act on a session running on another PC, which is display only.
- **Repeat reminders:** remind again every N minutes until the user acts. Make N a setting, or fix it.
- **Elevation:** if Herdr runs elevated and buZyist doesn't, the pipe may refuse the connection. Herdr's code has an "allow unelevated clients" setting; check how it's turned on.
- **Herdr not running:** fail quietly, with a status chip saying "Herdr not running", and retry with backoff.
- **Named sessions and remote machines:** Herdr can manage several sessions and SSH machines. Start with the default local session only.
- **No-Todoist mode:** with `use_todoist` off, sessions have no task id. Link the pane to the session itself instead.

## Open questions

1. **Is pausing while the agent works the right behaviour?** It means your focus time stops while the AI works. The other option is to keep the pomodoro running and only remind you when the agent needs you. That could be a setting.
2. **With several linked panes:** pause while *any* linked pane is working, or only the task's own pane?
3. **Reminders:** should `idle` (already seen) remind, or only `blocked` and `done`?
4. **Restart or resume:** when the user hits Play after an agent-caused pause, resume the paused session or start a new one? What if the pause outlasts the work phase?
5. **Connection method:** named pipe (no extra process, but more code to own) or the `herdr` CLI (simpler, but depends on `herdr` being on PATH)?
