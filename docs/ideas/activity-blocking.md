# Blocking per activity

Each activity (Email, Messages, ...) can block a different set of apps and
websites during its work phases. Decided on 2026-10-15.

## The rule

- A Todoist session, or a free session with no activity (just a typed name),
  uses the global settings as today.
- A session on an activity starts from the global settings and adjusts them,
  for apps and for websites separately:
  - `state` takes the switch: `inherit` (the global `block_apps` /
    `block_sites`), `off` (blocks nothing) or `on` (blocks even with the
    global switch off). The adjustments apply in `inherit` and `on`.
  - **Apps**: the global list (entries switched on) plus `block`, minus
    `allow`. Program names are exact, so `allow` just removes them;
    `PROTECTED_EXES` are refused in `block`.
  - **Websites**: `mode` is `""` (the global `site_mode`), `"block"` or
    `"allow"`. In block mode `block` adds to the list and `allow` carves
    exceptions out of it (`mail.google.com` gets through `*.google.com`); in
    allow mode `allow` adds to the allowed list and `block` carves blocks out
    of it. When the mode differs from the global one there is nothing to
    inherit: the list starts empty, so the field of that mode is the
    activity's own list (the UI says so).
- Worked out on every `/focus` and every blocker tick, so edits apply at once.
- Breaks block nothing, as today. Site and app limits don't change: they run
  all day, whatever the activity.

## Shape

Inside each activity, so removing one removes its rules. No `apps`/`sites` =
inherit everything, so existing configs stay valid.

```json
{"id": "email", "name": "Email", "icon": "mail",
 "apps":  {"state": "inherit", "allow": ["OUTLOOK.EXE"], "block": []},
 "sites": {"state": "inherit", "mode": "", "allow": ["mail.google.com"], "block": []}}
```

## Extension

`/focus` adds `except` (the carve-outs: allowed sites in block mode, blocked
ones in allow mode) and `activity` (its name, for the block page: "Blocked
during Email"). The extension turns `except` into rules one priority above its
focus rules and below the limit redirects; bump `manifest.json`'s version.
Extensions from before that get `patterns` without the entries equal to an
exception (best effort) and the "Reload extension" chip.

## UI

A "Global / Email / Messages ..." picker at the top of the Apps and Websites
tabs, reusing the editors. With an activity picked: the switch (as global /
off / on), the mode, and the resolved list, inherited entries in grey.

## Presets

New installs only (`DEFAULT_ACTIVITIES`, no migration): Email lets Outlook and
webmail through, Messages lets WhatsApp, Slack and Teams through, Call blocks
nothing, Programming and Reading inherit.

## Later

Todoist label -> activity; picking an activity when starting a task; changing
the activity mid-session; locking the lists during a work phase.
