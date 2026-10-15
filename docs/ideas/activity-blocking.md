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

## Presets

New installs only (`DEFAULT_ACTIVITIES`, no migration): Email lets Outlook and
webmail through, Messages lets WhatsApp, Slack and Teams through, Call blocks
nothing, Programming and Reading inherit.

## Status

Branch `activities` (not pushed). Commit `0f8b9eb` is the earlier work
(activities in free sessions, tomatoes from a task's duration); `220dd55` is
the backend of this feature:

- `activities.py`: `clean_app_rules` / `clean_site_rules`, called by
  `clean_activities` (errors prefixed with the activity's name; at start-up a
  broken rule set is dropped and the activity kept), the presets.
- `site_rules.py`: `activity_rules(cfg, id, key)` and `site_blocking(cfg, id)`
  -> `{on, mode, patterns, except}`. `blocker.py`: `app_blocking(cfg, id)` ->
  the entries to close/hide. (They sit there and not in `activities.py`
  because `activities.py` imports `blocker.py` for its validators.)
- `App.focus()` carries `activity` (id) and `activity_name`; `AppBlocker._loop`
  and `enforce(focus, apps)` use `app_blocking`; `site_focus` uses
  `site_blocking` and sends `except` and `activity` (name, "" when not active).
- Tests: `tests/test_activities.py` (section "what an activity blocks"), one
  in `tests/test_app_blocker.py`. All 984 pass, ruff clean.

Ticket 1 (the extension) is done, extension 1.9.0:

- `background.js`: `rulesOf` carries `except` ([] from older buZyists),
  `focusBlocks` (popup, badge, tab sweep) honours it. `buildRules`: in block
  mode an `allow` rule per exception at `EXCEPT_PRIORITY` 4 (wins the tie
  with the focus redirects), in allow mode a redirect per exception at
  `FOCUS_PRIORITY` 4 (above the allow rules); ids follow the patterns'.
  `LIMIT_PRIORITY` moved 3 -> 5, so limits beat exceptions and allow rules.
  CLAUDE.md's priority sentence updated.
- `blocked.js`: the eyebrow says "Blocked during Email" when the session has
  an activity (`activity` passed through `blockPageData`). Popup: the
  blocking line is prefixed with the activity's name (`popupData` passes it).
- Tests: `tests/js/rules.test.mjs` (limit priority 5; exceptions in both
  modes, limits over exceptions, `focusBlocks`).

Ticket 2 (the UI) is done:

- `ui/main.html`: a picker (`#appsScope`, `#sitesScope`) at the top of the
  Apps and Websites tabs; the global fields sit in `#appsGlobal` /
  `#sitesGlobal`, an activity's in `#appsActivity` / `#sitesActivity`.
- `ui/main.js`, section "blocking per activity": `blockScope` (shared by both
  tabs, reset when Settings open), `renderScopes` (also from
  `renderActivities` and a rename), `renderBlocking`, `renderActivityApps` /
  `renderActivitySites`. The rules are created on the first edit
  (`editActivityRules`), so looking at an activity doesn't change its config.
  Apps: the switch (`segButtons`, buttons, not named inputs, so `formValues`
  ignores them), the global entries switched on with a per-app switch
  (writes `allow`), "Also let through" for the `allow` names not in the list,
  "Also block" with the running-app suggestions (refuses `PROTECTED_EXES`).
  Websites: the switch, the mode ("Like global (block)" / "Block these" /
  "Allow only these"); in the global mode the global list with a switch per
  entry (writes the carve-out list), "Also block"/"Also allow" and "Let
  through"/"Still block"; in the other mode a note that nothing is inherited.
  The other carve-out list stays visible there too, since `site_blocking`
  still uses it. A name added to one list leaves the other.
- `appBlocking` / `siteBlocking` mirror the Python for the summary line
  under the lists; `tests/activity_blocking_cases.json` feeds
  `tests/test_activities.py` and `tests/js/activity_blocking.test.mjs`.
- Looked at in Edge headless with a stubbed `pywebview.api` (both tabs, both
  modes, Everything), and driven: toggles, adds, the protected refusal,
  focus kept, rename/remove updating the picker, the save payload.

Ticket 3: CLAUDE.md describes the `except` rules (Site blocking) and the
picker (Activities); the stray `nul` is deleted. Left on purpose: the
`__version__` bump (2.4.0 -> 2.5.0) and the PR into `main`, for when the
branch is to be published.

## Ticket 1: the extension (done)

`extension/background.js`, `buildRules` (around line 540) and the priorities
at the top (`CATCH_ALL 1`, `ALLOW 2`, `LIMIT 3`, `FOCUS 4`).

- **Allow mode**, `except` = sites blocked inside the allowed ones: a redirect
  rule per pattern at `FOCUS_PRIORITY` (above the allow rules at 2). Easy.
- **Block mode**, `except` = sites let through the blocked ones: an `allow`
  rule per pattern that beats the focus redirects (4) but not the limit
  redirects, so a site out of budget stays blocked. Not possible with today's
  numbers (limit 3 < focus 4). Recommended: an `EXCEPT` allow rule at 4 (at
  equal priority DNR prefers `allow` over `redirect`) and `LIMIT_PRIORITY`
  moved to 5. Side effect: a site both focus-blocked and over its limit shows
  the limit page instead of the focus page; the limit page's 2-minute pass
  then falls through to the focus redirect, which is fine. Update the
  CLAUDE.md sentence "Limit redirects (`?l=<ruleId>&u=`) sit at priority 3,
  above allow rules, below focus blocks in block mode (4)".
- Rule ids: `except` rules after the pattern ones, below `LIMIT_RULE_BASE`;
  keep `buildRules` pure (same answer -> same list).
- `matches`/`rulesOf` (around line 150, used for the popup and the badge):
  take `except` into account too.
- `blocked.js` `render`: with `focus.activity`, say "Blocked during Email"
  (only the wording; keep the task line and countdown). Check `blocked.html`
  for the element.
- Popup (`popup.js`, from the worker's `popupData`): show the activity's name
  next to the pomodoro if there is room; optional.
- Bump `version` in `extension/manifest.json` (1.8.0 -> 1.9.0); CI fails a PR
  that changes `extension/` without it. `EXTENSION_VERSION` follows the
  manifest, so older extensions get the "Reload extension" chip.
- Tests: `tests/js/` (node --test) if `buildRules` can be loaded there; see how
  the existing JS tests import code. At least `node --check extension/*.js`.

## Ticket 2: the UI (done)

`ui/main.html` (tabs `p_apps` line ~279, `p_sites` ~293) and `ui/main.js`
(`blockedApps` ~1542 / `renderBlockedApps`, `siteLists` ~1713, the activity
rows `activityRows` ~1319 / ~1447, save at ~2263 sends
`activities: activityRows`, which already keeps `apps`/`sites` since rows are
copied with `{...a}`).

- A picker at the top of the Apps and Websites tabs: "Everything (global)" and
  one entry per activity (icon + name), shared by both tabs.
- Global picked: the tabs as today.
- Activity picked: a three-way switch "Like the global setting / Don't block /
  Block" (`state`). Apps: the global entries shown greyed with a per-app "let
  through" toggle (writes `allow`), plus an "also block" input reusing
  `addBlockedApp`'s suggestions (writes `block`; refuse `PROTECTED_EXES`, as
  `addBlockedApp` does). Websites: mode "Like the global setting / Block these
  / Allow only these" (`mode`); in the global mode, the global list greyed
  plus "also block" and "let through" lists; in the other mode, a note that
  nothing is inherited and one list. Mirror `site_blocking` in JS for the
  resolved preview (or ask the backend: an `Api` method returning
  `site_blocking`/`app_blocking` for a draft config is simpler than a mirror,
  but the draft isn't saved yet; a mirror + shared JSON cases like
  `tests/plan_cases.json` is the house pattern).
- Pattern validation: `cleanSitePattern` already exists in `main.js`.
- The activity rows edited in General and in these tabs are the same objects
  (`activityRows`); a renamed or removed activity must update the picker.
- Look: `ui/style.css` tokens (`--bz-*`, role variables), no shadows, see
  CLAUDE.md "UI".

## Ticket 3: wrap-up

- CLAUDE.md: describe the extension's `except` rules (Site blocking
  paragraph) and the UI picker (Activities paragraph).
- Bump `__version__` in `busyist.py` (2.4.0 -> 2.5.0) when opening the PR;
  merging publishes it. PR from `activities` into `main`.
- Delete the stray `nul` file at the repo root (output of a `> nul` run in Git
  Bash; untracked, not committed).

## Open questions

- Lock the activity lists (and the global ones) during a work phase, so they
  can't be loosened mid-pomodoro? Today the global lists can be, so it was
  left as is.
- A free session with no activity uses the global settings; should there be a
  configurable default activity instead?

## Later

Todoist label -> activity; picking an activity when starting a task; changing
the activity mid-session.
