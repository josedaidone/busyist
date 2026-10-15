# Idea: Jira Cloud tickets as a task source

Status: **parked**. Scoped from the current code, nothing built yet.

## What was decided

- **Jira Cloud** only (`<site>.atlassian.net`). Data Center / Server is out of scope for now.
- **Jira and Todoist side by side.** Each can be turned on separately. When both are on, the task list has **two tabs, "Todoist" and "Jira"**. With only one on, there are no tabs. With neither on, buZyist stays in solo mode as today.
- **By default, buZyist does nothing to the ticket.** No comment, no label, no status change.
- **Optional, off by default:** when a session ends and the user completes the task, move the ticket to **Done** and add a comment with the pomodoros. This uses the existing "complete task" prompt.

## Access (no Jira admin needed)

- **Auth:** a personal **API token** (id.atlassian.com → Security → API tokens) plus the account **email**, sent as HTTP Basic (`email:token`). Any user can create one.
- **Permissions:** the token has exactly the user's own rights. buZyist can only see and transition issues the user could see and transition in the browser.
- **Storage:** `jira_token` goes in `SECRET_KEYS` (`secrets_dpapi.py`), DPAPI-encrypted like `todoist_token`. The email and site aren't secret.

Things that can still get in the way (only an org admin can change them):

- **Atlassian Guard** can block or limit API tokens for an organisation. Quick test: try to create a token and call `GET /rest/api/3/myself`.
- **IP allowlisting** (Premium/Enterprise) can refuse calls from off the company network or VPN.
- **Scoped API tokens** (newer) go through `https://api.atlassian.com/ex/jira/<cloudId>/...` instead of the site URL. Support classic tokens first; add scoped ones if needed.

## Jira REST calls needed

| buZyist need | Call | Notes |
| --- | --- | --- |
| Check the token, show who is signed in | `GET /rest/api/3/myself` | For the Settings "Test" button |
| List tasks of a saved query | `GET /rest/api/3/search/jql?jql=…&fields=summary,status,priority,duedate,labels,project,issuetype&nextPageToken=…` | The old `/rest/api/3/search` is deprecated/removed on Cloud; paginate with `nextPageToken` |
| Optional comment on Done | `POST /rest/api/2/issue/{key}/comment` with `{"body": "…"}` | v2 takes plain text; v3 needs Atlassian Document Format (ADF) JSON |
| Optional move to Done | `GET /rest/api/3/issue/{key}/transitions?expand=transitions.fields`, then `POST /rest/api/3/issue/{key}/transitions` with `{"transition": {"id": …}}` | See **Transitions** below |
| Open the ticket | `https://<site>.atlassian.net/browse/{key}` | In the browser, from the task row |

Rate limits: Jira answers `429` with `Retry-After`. Respect it and show the usual "couldn't reach" error.

### Transitions (the one tricky part)

Jira has no "close" call. Each project has its own workflow, so:

1. List the transitions the issue allows *now*.
2. Pick the one whose target status has `statusCategory.key == "done"`.
   - If there are several, take the one named "Done", else the first, and log the choice.
   - If there are none (e.g. the workflow requires "In Review" first), tell the user and offer **Open in Jira**.
3. If the transition requires fields (often `resolution`), fill `resolution` with "Done" when that value is allowed. Otherwise fall back to **Open in Jira** instead of guessing.
4. Add the comment *before* the transition: some workflows restrict comments on closed issues.

Later, maybe: a per-project choice of transition in Settings, remembered by project key.

## Planned design

1. **`buzyist/jira.py`:** a `Jira` client in the style of `todoist.py` (`call`, `pages`), with:
   - `tasks(jql)`, returning dicts shaped like Todoist tasks so the UI can share the row rendering: `id` = issue key (`ABC-123`), `content` = summary, plus `due`, `priority`, `labels` and `project`, and the `status`, `issuetype` and `url` extras
   - `comment(key, text)`
   - `complete(key)`
   - `myself()`
2. **A `source` on tasks** (`"todoist"` / `"jira"`) in:
   - `App.tasks`: key it by `(source, id)`, or prefix ids, so ids from the two services can't collide
   - `session["task"]`
   - `history.json` entries
   - the shared session (`App.shared_session`)

   `finish` and `complete_task` (`session.py`) dispatch on it. Today they assume Todoist whenever `task["id"]` is set.
   - Jira tasks: by default `finish` does nothing remote. `complete_task` runs comment + transition only when `jira_complete` is on.
   - Old history and sessions without `source` count as `"todoist"`.
3. **Config** (`DEFAULTS` + `Config` fields; no migration needed, these are new keys):
   - `use_jira` (False)
   - `jira_site` (validated `<name>.atlassian.net`)
   - `jira_email`
   - `jira_token` (secret)
   - `jira_filters`: a list of `{name, query}` with JQL. Default: `assignee = currentUser() AND statusCategory != Done ORDER BY priority DESC, updated DESC`
   - `jira_active_filter`
   - `jira_complete` (False): move to Done + comment on complete
4. **`todoist_on` gets a sibling, `jira_on`.** Solo mode becomes "neither is on". `Api.set_solo` and `body.solo` follow that.
5. **UI** (`ui/main.html|js`):
   - **Tabs:** Todoist / Jira tabs above the task list when both are on. Each tab has its own saved filters (Todoist filter queries, Jira JQL). Remember the last tab.
   - **Jira rows:** the issue key chip, the issue-type icon or text, and the status, with an "Open in Jira" link.
   - **Settings:** a Jira section with site, email, token (with the existing secret has/lost/Remove pattern), a **Test** button, the saved JQL queries (validated through `check_query` like Todoist's), and the `jira_complete` switch.
   - **End of session:** the completion prompt for a Jira task says "Move to Done" when `jira_complete` is on, and is hidden otherwise.
6. **Tests:** a `FakeJira` next to `FakeTodoist` (conftest), covering:
   - JQL paging
   - the transition picking cases: one Done, several, none, required resolution
   - `finish` making no Jira calls by default
   - Todoist + Jira sessions and history side by side
   - old history without `source`

Rough size: about 250 lines of Python for the client, about 150 for the source plumbing in `session.py`/`app.py`/`api.py`/`config.py`, about 200 of JS/HTML for tabs and settings, plus tests. **About 2–4 days**, plus about 1 day if transitions need the per-project choice.

## Considerations

- **Todoist is hard-wired today:** "Todoist" appears about 40 times in `ui/main.js` and 13 times in `main.html`, and `finish`, `complete_task` and `_label_task` (`session.py`) assume a Todoist id. The `source` plumbing is most of the work, more than the Jira client.
- **Labels:** the focus label (`focus_label`, `remove_label_on_end`) stays Todoist-only. Jira labels are visible to the whole team, so don't touch them by default.
- **Due dates and priorities:** Jira's `duedate` is a plain date and priorities are names ("High", "Medium"…). Map them to the existing `due_parts` display and the priority colors as closely as possible, or show the Jira name.
- **Offline / VPN:** the Jira tab shows the same "couldn't reach" state as Todoist and doesn't block Todoist or a solo session.
- **Remote sessions** (`sync_session`): the shared task carries `source`. Another PC without Jira still shows the name, display only.
- **Privacy:** ticket summaries can be confidential. They already go into `history.json`, the toast, and the shared session when `sync_session` is on; mention it in the Jira settings help text.

## Open questions

1. **Several Jira sites?** Assume one for now.
2. **Worklogs:** log each pomodoro as Jira work time (`POST /rest/api/3/issue/{key}/worklog`)? It would be a natural fit, but it was explicitly not wanted for now. It also needs time tracking enabled on the project.
3. **Move to "In Progress" when a session starts?** Not wanted for now, but it would reuse the transition code.
4. **Mixed view:** with both on, is there ever a need for one combined "All" tab?
5. **Herdr link** (see `herdr-integration.md`): if both ideas land, the task-to-pane link map must use the `(source, id)` key too.
