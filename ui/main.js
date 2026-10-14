"use strict";

const $ = (id) => document.getElementById(id);
const RING = 2 * Math.PI * 120;  // the ring's r in ui/main.html
const PRESETS = [
  { name: "Classic", work_minutes: 25, rest_minutes: 5, cycles: 4 },
  { name: "Short", work_minutes: 15, rest_minutes: 5, cycles: 4 },
  { name: "Long", work_minutes: 50, rest_minutes: 10, cycles: 3 },
  { name: "Deep", work_minutes: 90, rest_minutes: 15, cycles: 2 },
];

let api = null;
let state = null;          // last answer from get_state()
let tasks = [];
let shown = [];            // tasks after the search filter
let sel = 0;
let pomo = null;           // the pomodoro settings as edited here
let saveTimer = null;
let toastTimer = null;
let loadingTasks = false;
let lastTaskLoad = 0;
let filters = [];          // saved filters: [{name, query}]
let activeFilter = "";
let loadSeq = 0;           // newest task request; older answers are dropped
let editing = undefined;   // filter being edited: its name, null for a new one

// ------------------------------------------------------------- helpers

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}

function toast(message, bad) {
  const t = $("toast");
  t.textContent = message;
  t.className = "on" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), bad ? 7000 : 3200);
}

function clock(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function duration(min) {
  const h = Math.floor(min / 60), m = min % 60;
  return h ? (m ? `${h}h ${m}m` : `${h}h`) : `${m} min`;
}

function isoDay(offset) {
  const d = new Date();
  d.setDate(d.getDate() + (offset || 0));
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

function niceDay(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dueText(t) {
  if (!t.day) return "";
  if (t.overdue) return "Overdue · " + niceDay(t.day);
  if (t.day === isoDay(0)) return t.time || "";
  if (t.day === isoDay(1)) return "Tomorrow" + (t.time ? " " + t.time : "");
  return niceDay(t.day) + (t.time ? " " + t.time : "");
}

const prioClass = (p) => ({ 4: "p1", 3: "p2", 2: "p3" })[p] || "";
// Todoist's priorities (its API counts 4 as P1), with a word so the colour is never the only cue.
const PRIO_WORD = { p1: "Urgent", p2: "High", p3: "Medium" };

// One of the three tomatinhos: "done" (full), "now" (ring with a core), "plan" (outline).
function tomato(kind, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#t-" + kind);
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  svg.append(use);
  return svg;
}

// ---------------------------------------------------------- timer view

function timeLeft() {
  const t = state && state.timer;
  if (!t) return null;
  return t.paused ? t.left_ms : t.left_ms - (Date.now() - state.now);
}

function renderTimer() {
  if (!state) return;
  const t = state.timer, s = state.session;
  const phase = !t ? "idle" : t.paused ? "paused" : t.phase === "work" ? "work" : "rest";
  document.body.dataset.phase = phase;
  const p = pomo || state.pomodoro;

  // ring + time: the ring shows the time left and empties as it passes
  let left, total;
  if (t) {
    left = Math.max(0, timeLeft());
    total = t.total_ms;
  } else {
    left = total = p.work_minutes * 60000;
  }
  $("time").textContent = clock(left);
  $("ringProgress").style.strokeDasharray = RING;
  $("ringProgress").style.strokeDashoffset = t ? RING * (1 - left / total) : 0;
  $("timeLabel").textContent = !t ? "per tomato" : t.paused ? "paused" : "left";
  $("timerBox").setAttribute("aria-label", `${clock(left)} ${t ? "left" : "per tomato"}`);
  $("phaseTag").textContent = { idle: "Ready", paused: "Paused", work: "Focus", rest: "Break" }[phase];

  // this session's tomatoes: done, now, planned
  const rounds = t ? t.rounds : p.cycles;
  const done = !t ? 0 : t.round - (t.phase === "rest" ? 0 : 1);
  const now = t && t.phase === "work" ? t.round : 0;
  const key = `${rounds}/${done}/${now}`;
  const dots = $("rounds");
  if (dots.dataset.key !== key) {
    dots.dataset.key = key;
    dots.textContent = "";
    if (rounds <= 12) {
      for (let n = 1; n <= rounds; n++) dots.append(tomato(n <= done ? "done" : n === now ? "now" : "plan"));
    }
    dots.setAttribute("aria-label", t ? `Tomato ${Math.min(t.round, rounds)} of ${rounds}` : `${rounds} tomato${rounds === 1 ? "" : "es"}`);
  }
  const restMin = Math.round((t ? t.rest_ms : p.rest_minutes * 60000) / 60000);
  let line;
  if (!t) line = p.cycles === 1 ? `1 tomato of ${p.work_minutes} min, no break`
    : `${p.cycles} tomatoes of ${p.work_minutes} min, ${p.rest_minutes} min breaks`;
  else if (t.phase === "rest") line = `Break · tomato ${t.round + 1} of ${t.rounds} next`;
  else if (t.round >= t.rounds) line = `Tomato ${t.round} of ${t.rounds} · the last one`;
  else line = `Tomato ${t.round} of ${t.rounds} · ${restMin} min break after`;
  $("roundsText").textContent = line;
  document.title = t ? `${clock(left)} · ${s ? s.task.content : "buZyist"}` : "buZyist";
}

let wasRunning = null;
function renderCurrent() {
  const t = state.timer, s = state.session;
  const box = $("current");
  const meta = $("curMeta");
  meta.textContent = "";
  box.classList.toggle("idle", !s);
  $("controls").classList.toggle("hidden", !t);
  if (s) {
    $("curEyebrow").textContent = t
      ? (t.phase === "rest" ? `Break time. Since ${s.started_at}` : `${t.paused ? "Paused" : "Just this"} · since ${s.started_at}`)
      : "Wrapping up…";
    $("curTask").textContent = s.task.content;
    if (s.task.project) {
      const proj = el("span", "", s.task.project);
      meta.append(proj);
    }
    if (s.label_added && state.label) meta.append(el("span", "", "@" + state.label));
    if (s.task.url) {
      const open = el("button", "link", "Open in Todoist");
      open.onclick = () => api.open_url(s.task.url);
      meta.append(open);
    }
  } else if (t && state.remote) {
    // Started on another PC, which shared its task through Chrome sync.
    const r = state.remote;
    $("curEyebrow").textContent = `Started on ${r.by}` + (r.started_at ? ` · since ${r.started_at}` : "");
    $("curTask").textContent = r.task.content;
    if (r.task.project) meta.append(el("span", "", r.task.project));
    if (r.task.url) {
      const open = el("button", "link", "Open in Todoist");
      open.onclick = () => api.open_url(r.task.url);
      meta.append(open);
    }
  } else if (t) {
    $("curEyebrow").textContent = state.bar.local ? "No task linked" : "Started on the bar, no task linked";
    $("curTask").textContent = "Timer running";
    meta.append(el("span", "", state.todoist ? "Pick a task to put it in focus" : ""));
  } else {
    $("curEyebrow").textContent = state.todoist ? "One tomato at a time." : "Name it if you like, then go.";
    $("curTask").textContent = state.todoist ? "Pick a task to start" : "Ready when you are";
  }
  $("freeBox").classList.toggle("hidden", !!t || !!s);
  // One Tomate button per screen: with a task list, its play buttons are the main action.
  $("startFree").classList.toggle("primary", !state.todoist);
  if (t) {
    $("pauseIcon").setAttribute("href", t.paused ? "#i-play" : "#i-pause");
    $("btnPause").title = t.paused ? "Resume" : "Pause";
    $("btnPause").setAttribute("aria-label", $("btnPause").title);
  }
  $("setupHint").textContent = t ? "Applies to the next session" : "Used for the next session";
  // Settings stay open while idle and fold away once a timer runs.
  const running = !!t;
  if (running !== wasRunning) $("setup").open = !running;
  wasRunning = running;
}

function renderGuard() {
  const b = state.blocks;
  if (!b) return;
  const parts = [];
  if (b.apps) parts.push(`${b.apps} app${b.apps === 1 ? "" : "s"}`);
  if (b.sites_on) parts.push(b.site_mode === "allow" ? "allowlist" : `${b.sites} site${b.sites === 1 ? "" : "s"}`);
  if (b.limits) parts.push(`${b.limits} limit${b.limits === 1 ? "" : "s"}`);
  const count = (b.apps || 0) + (b.limits || 0) + (!b.sites_on ? 0 : b.site_mode === "allow" ? 1 : b.sites);
  const blocks = `${count} block${count === 1 ? "" : "s"}`;
  const guard = $("guard");
  let text;
  if (!parts.length) text = "Focus guard off · set it up";
  else if (b.active) text = `Focus guard on · ${blocks} active`;
  else text = `Focus guard ready · ${blocks}`;
  if ($("guardText").textContent !== text) $("guardText").textContent = text;
  guard.classList.toggle("on", parts.length > 0);
  guard.classList.toggle("active", !!b.active);
  $("guardIcon").setAttribute("href", b.active ? "#i-shield-check" : "#i-shield");
  guard.title = parts.length ? "What stays blocked during focus: " + parts.join(", ") : "Block distracting apps and websites during focus";
  guard.dataset.tab = b.apps || !b.sites_on ? "apps" : "sites";
}

function renderToday() {
  const today = state.today;
  const n = today.pomodoros;
  const date = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
  if ($("todayDate").textContent !== date) $("todayDate").textContent = date;
  $("sumTitle").textContent = n ? `${n} tomato${n === 1 ? "" : "es"} today` : "No tomatoes yet today";
  const side = [];
  if (today.minutes) side.push(duration(today.minutes) + " of focus");
  if (today.completed) side.push(`${today.completed} done`);
  $("sumSide").textContent = side.join(" · ");
  const box = $("sumTomatoes");
  const key = String(n);
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    box.textContent = "";
    if (!n) box.append(el("span", "none", "Pick a task. 25 minutes, just that."));
    for (let i = 0; i < Math.min(n, 16); i++) box.append(tomato("done"));
    if (n > 16) box.append(el("span", "more", `+${n - 16}`));
    box.setAttribute("aria-label", `${n} tomato${n === 1 ? "" : "es"} finished today`);
  }
  // Without the task list (no Todoist) the header chip carries the count.
  const chip = $("today");
  chip.textContent = "";
  chip.append(tomato("done", "t"), el("b", "", String(n)), n === 1 ? " tomato today" : " tomatoes today");
  if (today.minutes) chip.append(" · " + duration(today.minutes));
}

function renderChrome() {
  renderToday();
  renderGuard();

  const bar = state.bar;
  $("barChip").className = "chip" + (bar.ok === true ? " ok" : bar.ok === false ? " bad" : "");
  $("barText").textContent = bar.local ? "Timer on this PC"
    : bar.ok === true ? `BUSY Bar · ${bar.via}` : bar.ok === false ? "Bar offline" : "Looking for the bar";
  $("barChip").title = bar.ok === false ? bar.error : "";
  $("hotkeyHint").textContent = state.hotkey ? state.hotkey.replace(/\b\w/g, (c) => c.toUpperCase()) : "";

  const u = state.update;
  $("updateChip").classList.toggle("hidden", !u);
  if (u) $("updateChip").textContent = u.busy ? "Updating…" : `Update ${u.version}`;
  $("extChip").classList.toggle("hidden", !state.extension_outdated);
  if ($("drawer").classList.contains("on")) {
    renderUpdate();
    renderExtension(!!state.extension_connected, !!state.extension_outdated);
  }
}

function renderUpdate() {
  const u = state && state.update;
  const btn = $("installUpdate");
  btn.classList.toggle("hidden", !u);
  $("releaseNotes").classList.toggle("hidden", !u);
  if (!u) return;
  btn.textContent = u.busy ? "Installing…" : u.installable ? `Install ${u.version}` : `Download ${u.version}`;
  btn.disabled = u.busy;
  const out = $("updateResult");
  out.className = "test-result " + (u.error ? "bad" : "ok");
  out.textContent = u.error || (u.busy ? "Downloading the update…" : `buZyist ${u.version} is out. Installing it restarts buZyist.`);
}

function renderEnded() {
  const e = state.ended;
  $("endedWrap").classList.toggle("on", !!e);
  $("scrim").classList.toggle("on", !!e || $("drawer").classList.contains("on") || editing !== undefined);
  if (!e) return;
  const finished = e.reason === "finished";
  $("endedChip").textContent = finished ? "Done" : "Stopped";
  $("endedChip").className = "state-chip " + (finished ? "done-chip" : "stopped-chip");
  const box = $("endedTomatoes");
  box.textContent = "";
  for (let i = 0; i < Math.min(e.done, 8); i++) box.append(tomato("done"));
  if (e.done > 8) box.append(el("span", "more", `+${e.done - 8}`));
  box.classList.toggle("hidden", !e.done);
  box.setAttribute("aria-label", `${e.done} tomato${e.done === 1 ? "" : "es"}`);
  $("endedTitle").textContent = finished ? "Done. Take a breather." : "Session stopped";
  $("endedTask").textContent = e.task.content;
  $("endedComplete").classList.toggle("hidden", !e.task.id || !state.todoist);
  $("endedSummary").textContent = e.done
    ? `${e.done} tomato${e.done === 1 ? "" : "es"} · ${duration(e.minutes)} of focus`
    : "No full tomato this time. That's fine.";
  $("endedProblems").textContent = (e.problems || []).join(" ");
}

async function poll() {
  try {
    state = await api.get_state();
  } catch (err) {
    return;
  }
  if (!pomo) loadPomodoro(state.pomodoro);
  renderTimer();
  renderCurrent();
  renderChrome();
  renderEnded();
  renderTasksEnabled();
  markCurrentTask();
}

// The task list needs Todoist: grey it out without a token or when it's off.
let tasksWereOff = false;
let wasSolo = null;
function renderTasksEnabled() {
  // Todoist switched off: drop the task list and narrow the window.
  const solo = !state.use_todoist;
  document.body.classList.toggle("solo", solo);
  if (wasSolo !== null && solo !== wasSolo) api.set_solo(solo);
  wasSolo = solo;
  const off = !state.todoist;
  $("tasksCard").classList.toggle("off", off);
  for (const id of ["search", "refresh", "editFilter", "addFilter"]) $(id).disabled = off;
  if (off && !tasksWereOff) {
    tasks = [];
    $("filterTabs").textContent = "";
    $("filterText").textContent = "";
    showEmpty("Todoist is off",
      state.needs_setup ? "Add your Todoist API token to see your tasks." : "Turn it on in Settings to pick tasks.");
  }
  if (!off && tasksWereOff) loadTasks();
  tasksWereOff = off;
}

// --------------------------------------------------------------- tasks

function skeleton() {
  const list = $("list");
  list.textContent = "";
  for (let i = 0; i < 6; i++) list.append(el("li", "skeleton"));
}

async function loadTasks(quiet, filterName) {
  if (state && !state.todoist) return;
  if (loadingTasks && filterName === undefined) return;  // a refresh is already on its way
  const seq = ++loadSeq;
  loadingTasks = true;
  $("refresh").classList.add("spin");
  if (!quiet || !tasks.length) skeleton();
  const result = await api.get_tasks(filterName === undefined ? null : filterName);
  if (seq !== loadSeq) return;  // the user picked another filter meanwhile
  applyTaskResult(result);
}

function applyTaskResult(result) {
  loadingTasks = false;
  lastTaskLoad = Date.now();
  $("refresh").classList.remove("spin");
  if (!result.ok) {
    tasks = [];
    renderFilters();
    showEmpty("Couldn't load tasks", result.error, /token/i.test(result.error));
    return;
  }
  tasks = result.tasks;
  filters = result.filters;
  activeFilter = result.active;
  renderFilters();
  renderTasks();
}

// ------------------------------------------------------------- filters

function renderFilters(loadingName) {
  const box = $("filterTabs");
  box.textContent = "";
  filters.forEach((f, i) => {
    const b = el("button", "ftab" + (f.name === activeFilter ? " on" : "") + (f.name === loadingName ? " loading" : ""));
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", f.name === activeFilter);
    b.title = f.query + (i < 9 ? `  (Ctrl+${i + 1})` : "");
    b.append(f.name);
    if (i < 9) b.append(el("span", "n", String(i + 1)));
    b.onclick = () => selectFilter(f.name);
    b.ondblclick = () => openFilterEditor(f.name);
    box.append(b);
    if (f.name === activeFilter) setTimeout(() => b.scrollIntoView({ block: "nearest", inline: "nearest" }));
  });
  const active = filters.find((f) => f.name === activeFilter);
  $("filterText").textContent = active ? active.query : "";
  $("filterText").title = active ? active.query : "";
  $("editFilter").disabled = !active;
}

function selectFilter(name) {
  if (name === activeFilter && !loadingTasks) return;
  activeFilter = name;
  sel = 0;
  renderFilters(name);
  loadTasks(false, name);
}

const EXAMPLES = ["today | overdue", "7 days", "p1 | p2", "#Inbox", "@waiting", "no date", "assigned to: me", "!@waiting"];

function openFilterEditor(name) {
  const f = name ? filters.find((x) => x.name === name) : null;
  editing = f ? f.name : null;
  $("filterTitle").textContent = f ? "Edit filter" : "New filter";
  $("f_name").value = f ? f.name : "";
  $("f_query").value = f ? f.query : "";
  $("filterTest").textContent = "";
  $("filterTest").className = "test-result";
  $("filterDelete").classList.toggle("hidden", !f || filters.length <= 1);
  $("filterLeft").classList.toggle("hidden", !f || filters.length <= 1);
  $("filterRight").classList.toggle("hidden", !f || filters.length <= 1);
  const ex = $("examples");
  ex.textContent = "";
  for (const q of EXAMPLES) {
    const b = el("button", "", q);
    b.type = "button";
    b.title = "Add to the query";
    b.onclick = () => {
      const area = $("f_query");
      const cur = area.value.trim();
      area.value = cur ? `(${cur}) & ${q}` : q;
      area.focus();
    };
    ex.append(b);
  }
  $("filterWrap").classList.add("on");
  $("scrim").classList.add("on");
  setTimeout(() => (f ? $("f_query") : $("f_name")).focus(), 50);
}

function closeFilterEditor() {
  editing = undefined;
  $("filterWrap").classList.remove("on");
  if (!(state && state.ended) && !$("drawer").classList.contains("on")) $("scrim").classList.remove("on");
}

function filterBusy(on) {
  for (const id of ["filterSave", "filterPreview", "filterDelete"]) $(id).disabled = on;
}

function filterMessage(text, ok) {
  $("filterTest").className = "test-result " + (ok ? "ok" : "bad");
  $("filterTest").textContent = text;
}

$("addFilter").onclick = () => openFilterEditor(null);
$("editFilter").onclick = () => openFilterEditor(activeFilter);
$("filterCancel").onclick = closeFilterEditor;
$("filterPreview").onclick = async () => {
  filterBusy(true);
  filterMessage("Asking Todoist…", true);
  const r = await api.preview_filter($("f_query").value);
  filterBusy(false);
  if (r.ok) filterMessage(`${r.count} task${r.count === 1 ? "" : "s"} match`, true);
  else filterMessage(r.error, false);
};
$("filterForm").onsubmit = async (e) => {
  e.preventDefault();
  filterBusy(true);
  filterMessage("Checking the filter with Todoist…", true);
  const r = await api.save_filter(editing, $("f_name").value, $("f_query").value);
  filterBusy(false);
  if (!r.ok) return filterMessage(r.error, false);
  ++loadSeq;  // this answer already carries the new list
  closeFilterEditor();
  sel = 0;
  applyTaskResult(r);
  toast(`Filter “${r.active}” saved`);
};
$("filterDelete").onclick = async () => {
  if (!confirm(`Delete the filter “${editing}”?`)) return;
  filterBusy(true);
  const r = await api.delete_filter(editing);
  filterBusy(false);
  if (!r.ok) return filterMessage(r.error, false);
  ++loadSeq;
  closeFilterEditor();
  applyTaskResult(r);
  toast("Filter deleted");
};
async function moveFilter(delta) {
  const r = await api.move_filter(editing, delta);
  if (!r.ok) return filterMessage(r.error, false);
  filters = r.filters;
  renderFilters();
}
$("filterLeft").onclick = () => moveFilter(-1);
$("filterRight").onclick = () => moveFilter(1);
$("filterTabs").addEventListener("wheel", (e) => {
  // a vertical wheel scrolls the tabs sideways
  if (e.deltaY) { e.preventDefault(); $("filterTabs").scrollLeft += e.deltaY; }
}, { passive: false });

function showEmpty(title, text, withSettings) {
  const list = $("list");
  list.textContent = "";
  const li = el("li", "empty");
  li.append(el("b", "", title), el("span", "", text));
  if (withSettings) {
    const b = el("button", "btn", "Open settings");
    b.style.marginTop = "14px";
    b.onclick = () => openSettings("todoist");
    li.append(el("br"), b);
  }
  list.append(li);
  $("count").textContent = "";
}

function renderTasks() {
  const q = $("search").value.trim().toLowerCase();
  shown = !q ? tasks : tasks.filter((t) =>
    (t.content + " " + t.project + " " + t.labels.join(" ")).toLowerCase().includes(q));
  sel = Math.min(sel, Math.max(0, shown.length - 1));
  $("count").textContent = `${shown.length} of ${tasks.length}`;
  const list = $("list");
  list.textContent = "";
  if (!tasks.length) return showEmpty("All clear", "Nothing in this filter. Enjoy it.");
  if (!shown.length) return showEmpty("No match", `No task contains “${$("search").value}”.`);

  shown.forEach((t, i) => {
    const li = el("li", "task");
    li.dataset.id = t.id;
    const body = el("div", "t-body");
    body.append(el("div", "t-title", t.content));
    const meta = el("div", "t-meta");
    if (t.project && !t.inbox) {
      const proj = el("span", "proj");
      const sw = el("i");
      sw.style.background = t.color;
      proj.append(sw, t.project);
      meta.append(proj);
    }
    const due = dueText(t);
    if (due) meta.append(el("span", t.overdue ? "late" : "", due));
    for (const l of t.labels) meta.append(el("span", "lbl" + (l === state?.label ? " mine" : ""), "@" + l));
    const dots = el("span", "t-dots");
    meta.prepend(dots);
    // Priority: a pill (filled flag, P1-P3 and a word) first in the meta line,
    // and the checkbox takes the same colour, like in Todoist.
    const prio = prioClass(t.priority);
    if (prio) {
      li.classList.add(prio);
      const pill = el("span", "prio-pill " + prio);
      pill.title = `Priority ${prio.slice(1)} in Todoist`;
      pill.append(icon("flag-fill"), el("b", "", prio.toUpperCase()), PRIO_WORD[prio]);
      meta.prepend(pill);
    }
    body.append(meta);
    const play = el("button", "play");
    play.append(icon("play"));
    play.title = "Start a tomato";
    play.setAttribute("aria-label", "Start a tomato on: " + t.content);
    play.tabIndex = -1;
    const check = el("button", "prio");
    check.title = "Mark as done";
    check.setAttribute("aria-label", "Mark as done: " + t.content + (prio ? ` (priority ${prio.slice(1)})` : ""));
    check.tabIndex = -1;
    const box = el("span", "box");
    box.append(icon("check"));
    check.append(box);
    check.onclick = (e) => { e.stopPropagation(); completeTask(t, li); };
    li.append(check, body, play);
    play.onclick = (e) => { e.stopPropagation(); startTask(t, li); };
    li.onclick = (e) => { sel = i; markSelection(false); openTaskMenu(t, li, e.clientX, e.clientY); };
    li.oncontextmenu = (e) => {
      e.preventDefault();
      sel = i;
      markSelection(false);
      openTaskMenu(t, li, e.clientX, e.clientY);
    };
    li.onmousemove = () => { if (sel !== i) { sel = i; markSelection(false); } };
    list.append(li);
  });
  markSelection(false);
  markCurrentTask();
}

function markSelection(scroll) {
  [...$("list").querySelectorAll(".task")].forEach((li, i) => {
    li.classList.toggle("sel", i === sel);
    if (i === sel && scroll) li.scrollIntoView({ block: "nearest" });
  });
}

// The task in focus gets a Tomate border and a state badge; every row shows
// one small tomato per pomodoro done on it.
function markCurrentTask() {
  const id = state && state.session && state.session.task.id;
  const t = state && state.timer;
  const word = !t ? "Wrapping up" : t.paused ? "Paused" : t.phase === "rest" ? "Break" : "In focus";
  const counts = (state && state.task_tomatoes) || {};
  for (const li of $("list").querySelectorAll(".task")) {
    const on = li.dataset.id === id;
    li.classList.toggle("in-focus", on);
    let badge = li.querySelector(".badge");
    if (on && !badge) {
      badge = el("span", "badge", word);
      li.insertBefore(badge, li.querySelector(".play"));
    } else if (!on && badge) {
      badge.remove();
    }
    if (badge && badge.textContent !== word) badge.textContent = word;
    const dots = li.querySelector(".t-dots");
    const n = counts[li.dataset.id] || 0;
    if (dots && dots.dataset.n !== String(n)) {
      dots.dataset.n = String(n);
      dots.textContent = "";
      for (let k = 0; k < Math.min(n, 8); k++) dots.append(el("i"));
      if (n > 8) dots.append(el("span", "", "+" + (n - 8)));
      dots.classList.toggle("hidden", !n);
      dots.setAttribute("role", "img");
      dots.setAttribute("aria-label", `${n} tomato${n === 1 ? "" : "es"} done`);
    }
  }
}

async function startTask(task, li) {
  if (state && state.session && state.session.task.id === task.id) {
    toast("That task is already in focus.");
    return;
  }
  if (state && state.session && !confirm(`Replace the current session on “${state.session.task.content}”?`)) return;
  if (li) li.classList.add("busy");
  flushPomodoro();
  const result = await api.start(task.id);
  if (li) li.classList.remove("busy");
  if (!result.ok) return toast(result.error, true);
  toast(`Go. ${pomo.work_minutes} minutes, just this${state && state.bar.local ? "" : " (on the bar)"}.`);
  $("search").value = "";
  renderTasks();
  poll();
}

async function completeTask(task, li) {
  const inFocus = state && state.session && state.session.task.id === task.id;
  if (inFocus && !confirm(`“${task.content}” is in focus. Stop the session and mark it as done?`)) return;
  if (li) li.classList.add("busy", "done");
  if (inFocus) {
    const stopped = await api.control("stop");
    if (!stopped.ok) { if (li) li.classList.remove("busy", "done"); return toast(stopped.error, true); }
  }
  const result = await api.complete(task.id);
  if (!result.ok) {
    if (li) li.classList.remove("busy", "done");
    return toast(result.error, true);
  }
  toast("Done in Todoist.");
  tasks = tasks.filter((t) => t.id !== task.id);
  renderTasks();
  poll();
}

// ------------------------------------------------------------ task menu

function closeTaskMenu() {
  const m = $("taskMenu");
  if (m) m.remove();
}

function openTaskMenu(task, li, x, y) {
  closeTaskMenu();
  const menu = el("div", "ctx-menu");
  menu.id = "taskMenu";
  menu.setAttribute("role", "menu");
  const item = (ic, label, fn) => {
    const b = el("button", "ctx-item");
    b.setAttribute("role", "menuitem");
    b.append(icon(ic), label);
    b.onclick = (e) => { e.stopPropagation(); closeTaskMenu(); fn(); };
    menu.append(b);
  };
  item("play", "Start pomodoro", () => startTask(task, li));
  item("check", "Mark as done", () => completeTask(task, li));
  if (task.url) item("eye", "See on Todoist", () => api.open_url(task.url));
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(4, Math.min(x, innerWidth - r.width - 4)) + "px";
  menu.style.top = Math.max(4, Math.min(y, innerHeight - r.height - 4)) + "px";
  menu.querySelector("button").focus();
}

document.addEventListener("mousedown", (e) => { if (!e.target.closest("#taskMenu")) closeTaskMenu(); });
document.addEventListener("contextmenu", (e) => { if (!e.target.closest(".task")) closeTaskMenu(); });
window.addEventListener("blur", closeTaskMenu);
window.addEventListener("resize", closeTaskMenu);
document.addEventListener("scroll", closeTaskMenu, true);

// ------------------------------------------------------------ pomodoro

function loadPomodoro(p) {
  pomo = { work_minutes: p.work_minutes, rest_minutes: p.rest_minutes, cycles: p.cycles, autostart: p.autostart };
  renderPomodoro();
}

function renderPomodoro() {
  for (const box of document.querySelectorAll(".stepper")) {
    const input = box.querySelector("input");
    if (document.activeElement !== input) input.value = pomo[box.dataset.key];
  }
  $("autostart").checked = pomo.autostart;
  const presets = $("presets");
  presets.textContent = "";
  for (const p of PRESETS) {
    const on = p.work_minutes === pomo.work_minutes && p.rest_minutes === pomo.rest_minutes && p.cycles === pomo.cycles;
    const b = el("button", "preset" + (on ? " on" : ""), `${p.work_minutes} / ${p.rest_minutes}`);
    b.title = `${p.name}: ${p.cycles} × ${p.work_minutes} min focus, ${p.rest_minutes} min breaks`;
    b.onclick = () => {
      Object.assign(pomo, { work_minutes: p.work_minutes, rest_minutes: p.rest_minutes, cycles: p.cycles });
      changed();
    };
    presets.append(b);
  }
  const minutes = pomo.cycles * pomo.work_minutes + (pomo.cycles - 1) * pomo.rest_minutes;
  const end = new Date(Date.now() + minutes * 60000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  $("total").textContent = `${duration(minutes)} · ends ~${end}`;
  if (state) renderTimer();
}

function changed() {
  renderPomodoro();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(savePomodoro, 500);
}

async function savePomodoro() {
  saveTimer = null;
  const result = await api.set_pomodoro(pomo.work_minutes, pomo.rest_minutes, pomo.cycles, pomo.autostart);
  if (!result.ok) toast(result.error, true);
}

function flushPomodoro() {
  if (saveTimer) {
    clearTimeout(saveTimer);
    savePomodoro();
  }
}

function clampStepper(box, value) {
  const min = +box.dataset.min, max = +box.dataset.max;
  return Math.max(min, Math.min(max, Math.round(value)));
}

for (const box of document.querySelectorAll(".stepper")) {
  const key = box.dataset.key, step = +box.dataset.step;
  const input = box.querySelector("input");
  for (const b of box.querySelectorAll("button")) {
    b.onclick = () => {
      const d = +b.dataset.d;
      let v = pomo[key] + d * step;
      // snap to the step grid so 27 + 5 goes to 30, not 32
      if (step > 1) v = d > 0 ? Math.floor(pomo[key] / step) * step + step : Math.ceil(pomo[key] / step) * step - step;
      pomo[key] = clampStepper(box, v);
      changed();
    };
  }
  input.addEventListener("change", () => {
    const v = parseInt(input.value, 10);
    pomo[key] = isNaN(v) ? pomo[key] : clampStepper(box, v);
    input.value = pomo[key];
    changed();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.blur();
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      box.querySelector(`button[data-d="${e.key === "ArrowUp" ? 1 : -1}"]`).click();
    }
    e.stopPropagation();
  });
  input.addEventListener("wheel", (e) => {
    e.preventDefault();
    box.querySelector(`button[data-d="${e.deltaY < 0 ? 1 : -1}"]`).click();
  }, { passive: false });
}
$("autostart").onchange = () => { pomo.autostart = $("autostart").checked; changed(); };

// ------------------------------------------------------------- controls

async function control(action) {
  const result = await api.control(action);
  if (!result.ok) toast(result.error, true);
  poll();
}
async function startFree(name) {
  if (typeof name !== "string") name = $("freeName").value;
  if (state && state.session && !confirm(`Replace the current session on “${state.session.task.content}”?`)) return;
  flushPomodoro();
  const result = await api.start(null, name);
  if (!result.ok) return toast(result.error, true);
  toast(`Go. ${pomo.work_minutes} minutes, just this${state && state.bar.local ? "" : " (on the bar)"}.`);
  $("freeName").value = "";
  poll();
}
$("startFree").onclick = () => startFree();
$("freeName").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.stopPropagation(); startFree(); } });
$("btnStop").onclick = () => control("stop");
$("guard").onclick = () => openSettings($("guard").dataset.tab || "apps");
$("btnSkip").onclick = () => control("skip");
$("btnPause").onclick = () => control(state.timer && state.timer.paused ? "resume" : "pause");

$("endedClose").onclick = async () => { await api.dismiss_ended(); poll(); };
$("endedComplete").onclick = async () => {
  const e = state.ended;
  const result = await api.complete(e.task.id);
  if (!result.ok) return toast(result.error, true);
  toast("Done in Todoist.");
  // drop it at once, then reload so the filter shows what Todoist now has
  tasks = tasks.filter((t) => t.id !== e.task.id);
  renderTasks();
  await poll();
  loadTasks(true);
};
$("endedAgain").onclick = async () => {
  const e = state.ended;
  const task = tasks.find((t) => t.id === e.task.id);
  await api.dismiss_ended();
  if (!e.task.id) return startFree(e.task.content);
  if (!task) return toast("That task is no longer in the list.", true);
  startTask(task);
};

// ------------------------------------------------------------- settings

// ---------------------------------------------------------- settings tabs

const TABS = [...document.querySelectorAll("#settingsTabs .stab")].map((b) => b.dataset.tab);
let settingsTab = localStorage.getItem("settingsTab") || "todoist";

function showTab(name, focus) {
  if (!TABS.includes(name)) name = "todoist";
  settingsTab = name;
  localStorage.setItem("settingsTab", name);
  for (const b of document.querySelectorAll("#settingsTabs .stab")) {
    const on = b.dataset.tab === name;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", on);
    b.tabIndex = on ? 0 : -1;
    if (on && focus) b.focus();
  }
  for (const p of document.querySelectorAll("[data-panel]")) p.classList.toggle("hidden", p.dataset.panel !== name);
  $("settingsForm").scrollTop = 0;
}

for (const b of document.querySelectorAll("#settingsTabs .stab")) b.onclick = () => showTab(b.dataset.tab);
$("settingsTabs").addEventListener("keydown", (e) => {
  const i = TABS.indexOf(settingsTab);
  const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  showTab(TABS[(next + TABS.length) % TABS.length], true);
});

// Small dots on the tabs that need a look: an update, or site blocking
// switched on without the extension connected.
function renderTabDots() {
  $("aboutDot").classList.toggle("hidden", !(state && state.update));
  const sitesOn = $("s_block_sites").checked;
  const connected = !!(state && state.extension_connected && !state.extension_outdated);
  $("sitesDot").classList.toggle("hidden", !sitesOn || connected);
  $("limitsDot").classList.toggle("hidden", !$("s_site_limits_on").checked || connected);
}

async function openSettings(tab) {
  const s = await api.get_settings();
  const form = $("settingsForm");
  for (const input of form.querySelectorAll("input[name]")) {
    if (input.type === "checkbox") input.checked = !!s[input.name];
    else if (input.type === "radio") input.checked = s[input.name] === input.value;
    else input.value = s[input.name] ?? "";
  }
  $("testResult").textContent = "";
  showSecrets(s);
  blockedApps =(s.blocked_apps || []).map((a) => ({ ...a, exes: [...a.exes] }));
  $("addBlockedApp").value = "";
  renderBlockedApps();
  siteLists = { block: [...(s.blocked_sites || [])], allow: [...(s.allowed_sites || [])] };
  LIMITS.site.rules = JSON.parse(JSON.stringify(s.site_limits || []));
  LIMITS.app.rules = JSON.parse(JSON.stringify(s.app_limits || []));
  LIMITS.site.usage = {};
  LIMITS.app.usage = {};
  closeLimitEditor();
  renderLimits();
  refreshLimitUsage();
  syncOwner = s.sync_owner || "";
  renderSync(s.sync_info);
  $("addBlockedSite").value = "";
  $("extDir").textContent = s.extension_dir || "extension";
  $("extDir").title = s.extension_dir || "";
  for (const n of document.querySelectorAll(".browser-name")) n.textContent = s.extension_browser || "Chrome";
  renderBlockedSites();
  $("extOldVersion").textContent = s.extension_version ? "version " + s.extension_version : "an older version";
  $("extNewVersion").textContent = s.extension_latest || "";
  renderExtension(s.extension_connected, s.extension_outdated);
  showBarFields();
  showTodoistFields();
  $("aboutVersion").textContent = "buZyist " + s.version;
  $("aboutData").textContent = s.data_dir;
  $("autoUpdateRow").classList.toggle("hidden", !s.can_auto_update);
  $("updateResult").textContent = "";
  renderUpdate();
  if (s.use_todoist && !s.has_todoist_token) tab = "todoist";
  showTab(typeof tab === "string" ? tab : settingsTab);
  $("drawer").classList.add("on");
  $("scrim").classList.add("on");
  setTimeout(() => {
    if (s.use_todoist && !s.has_todoist_token) $("s_todoist_token").focus();
    else document.querySelector("#settingsTabs .stab.on").focus();
  }, 220);
}

// The token and the PIN never come to the page (get_settings sends "" and
// has_*). An empty field keeps the saved one; Remove asks Python to clear it.
const SECRETS = {
  todoist_token: { input: "s_todoist_token", saved: "tokenSaved", lost: "tokenLost", remove: "removeToken", empty: "Paste your API token" },
  busybar_pin: { input: "s_busybar_pin", saved: "pinSaved", lost: "pinLost", remove: "removePin", empty: "" },
};
let secretsToClear = {};

function showSecrets(s) {
  secretsToClear = {};
  for (const [key, x] of Object.entries(SECRETS)) {
    const has = !!s["has_" + key];
    $(x.input).value = "";
    $(x.input).placeholder = has ? "•••••••• saved (type to replace)" : x.empty;
    $(x.saved).classList.toggle("hidden", !has);
    $(x.lost).classList.toggle("hidden", !s[key + "_lost"]);
  }
}

// What a save sends for the secrets: Remove, unless a new one was typed.
function secretClears() {
  const out = {};
  for (const [key, x] of Object.entries(SECRETS)) if (secretsToClear[key] && !$(x.input).value.trim()) out["clear_" + key] = true;
  return out;
}

for (const [key, x] of Object.entries(SECRETS)) {
  $(x.remove).onclick = () => {
    secretsToClear[key] = true;
    $(x.input).value = "";
    $(x.input).placeholder = x.empty;
    $(x.saved).classList.add("hidden");
    $(x.input).focus();
  };
}

// The token and label options only matter with Todoist.
function showTodoistFields() {
  const on = $("s_use_todoist").checked;
  $("todoistFields").classList.toggle("muted", !on);
  $("noTodoistHelp").classList.toggle("hidden", on);
}
$("s_use_todoist").onchange = showTodoistFields;

// The bar's address, PIN and polling only matter with a bar.
function showBarFields() {
  const on = $("s_use_busybar").checked;
  $("barFields").classList.toggle("hidden", !on);
  $("noBarHelp").classList.toggle("hidden", on);
}
$("s_use_busybar").onchange = showBarFields;

// The apps kept away during focus, as edited here; saved with the rest.
// Each is {id, name, exes, on, action: "close"|"hide", custom}; the presets
// come from Python and can only be switched, apps added here can be removed.
const MAX_BLOCKED_APPS = 20;
let blockedApps = [];

function renderBlockedApps() {
  const list = $("blockedApps");
  list.replaceChildren();
  blockedApps.forEach((app, i) => {
    const row = el("div", "app-row" + (app.on ? "" : " off"));

    const label = el("label", "switch");
    const check = el("input");  // no name: formValues() leaves it alone
    check.type = "checkbox";
    check.checked = app.on;
    check.onchange = () => { app.on = check.checked; row.classList.toggle("off", !app.on); };
    label.append(check, el("span", "knob"), el("span", "", app.name), el("span", "exe", app.exes[0]));
    label.title = app.exes.join(", ");

    const action = el("select", "action");
    for (const [value, text] of [["close", "Close"], ["hide", "Hide to tray"]]) {
      const opt = el("option", "", text);
      opt.value = value;
      action.append(opt);
    }
    action.value = app.action;
    action.onchange = () => { app.action = action.value; };

    row.append(label, action);
    if (app.custom) {
      const x = el("button", "remove", "×");
      x.type = "button";
      x.title = "Remove " + app.name;
      x.onclick = () => { blockedApps.splice(i, 1); renderBlockedApps(); };
      row.append(x);
    } else {
      row.append(el("span"));
    }
    list.append(row);
  });
  $("blockedAppsFields").classList.toggle("muted", !$("s_block_apps").checked);
}

// Programs the blocker never closes: PROTECTED_EXES in buzyist/blocker.py, which
// has the last word when Settings are saved.
const PROTECTED_EXES = new Set([
  "explorer", "svchost", "csrss", "wininit", "winlogon", "lsass", "services", "smss",
  "dwm", "fontdrvhost", "sihost", "taskhostw", "ctfmon", "runtimebroker", "searchhost",
  "startmenuexperiencehost", "shellexperiencehost", "textinputhost", "taskmgr",
  "conhost", "dllhost", "audiodg", "spoolsv", "msmpeng", "securityhealthservice",
  "buzyist", "busyist", "python", "pythonw", "py",
  "chrome", "msedge", "msedgewebview2",
]);

function addBlockedApp() {
  const input = $("addBlockedApp");
  const name = input.value.trim();
  if (!name) return;
  if (/[\\/]/.test(name)) return toast("App names are just the program, like WhatsApp.exe.", true);
  const key = (n) => n.toLowerCase().replace(/\.exe$/, "");
  if (PROTECTED_EXES.has(key(name))) {
    return toast(`buZyist won't close ${name}: Windows or your browser needs it.`, true);
  }
  const known = blockedApps.find((a) => a.exes.some((e) => key(e) === key(name)));
  input.value = "";
  if (known) { known.on = true; return renderBlockedApps(); }  // already listed: just switch it on
  if (blockedApps.filter((a) => a.custom).length >= MAX_BLOCKED_APPS) {
    return toast(`Keep the added apps under ${MAX_BLOCKED_APPS}.`, true);
  }
  const exe = /\.exe$/i.test(name) ? name : name + ".exe";
  blockedApps.push({
    id: "custom:" + key(exe), name: exe.replace(/\.exe$/i, "").split(".")[0] || exe,
    exes: [exe], on: true, action: "close", custom: true,
  });
  renderBlockedApps();
}
$("addBlockedAppBtn").onclick = addBlockedApp;
$("addBlockedApp").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); addBlockedApp(); }
});
$("s_block_apps").onchange = renderBlockedApps;

// The website lists, one per mode: blocked during focus, or the only ones
// allowed. Entries are domains or URL patterns with * wildcards; the Chrome
// extension reads them from buZyist. Edited here, saved with the rest.
let siteLists = { block: [], allow: [] };
const siteMode = () => (document.querySelector('input[name="site_mode"]:checked') || {}).value || "block";

function renderBlockedSites() {
  const mode = siteMode();
  const sites = siteLists[mode];
  const list = $("blockedSites");
  list.replaceChildren();
  $("addBlockedSite").placeholder = mode === "allow"
    ? "Add an allowed website, e.g. github.com or *.google.com"
    : "Add a website, e.g. youtube.com or *.reddit.com";
  $("sitesHelp").textContent = mode === "allow"
    ? (sites.length ? "During work phases every other website shows a \"back to work\" page."
                    : "The list is empty, so every website is blocked during work phases.")
    : "These websites show a \"back to work\" page during work phases.";
  sites.forEach((site, i) => {
    const row = el("div", "app-row");
    row.append(el("span", "site", site));
    const x = el("button", "remove", "\u00d7");
    x.type = "button";
    x.title = "Remove " + site;
    x.onclick = () => { sites.splice(i, 1); renderBlockedSites(); };
    row.append(el("span"), x);
    list.append(row);
  });
  $("blockedSitesFields").classList.toggle("muted", !$("s_block_sites").checked);
  renderTabDots();
}

// The setup steps show until the extension has called buZyist.
// A connected extension older than the one buZyist ships needs a reload.
function renderExtension(connected, outdated) {
  const status = $("extStatus");
  const good = connected && !outdated;
  status.textContent = !connected ? "Browser extension: not connected"
    : outdated ? "Browser extension: needs a reload" : "Browser extension: connected";
  status.classList.toggle("ok", good);
  status.classList.toggle("bad", !good);
  $("extSetup").classList.toggle("hidden", connected);
  $("extOutdated").classList.toggle("hidden", !outdated);
  $("extDone").classList.toggle("hidden", !good);
  renderTabDots();
}

function addBlockedSite() {
  const input = $("addBlockedSite");
  const site = cleanSitePattern(input.value);
  input.value = "";
  if (site === "") return;
  if (site === null) return toast("Type a website like youtube.com or *.google.com.", true);
  const sites = siteLists[siteMode()];
  if (sites.includes(site)) return;
  if (sites.length >= 50) return toast("Keep the list under 50 websites.", true);
  sites.push(site);
  renderBlockedSites();
}
// Mirrors clean_site_pattern in buzyist/site_rules.py, which has the final say on save.
// "" for nothing typed, null for something that isn't a website.
function cleanSitePattern(raw) {
  let text = String(raw).trim().toLowerCase();
  if (!text) return "";
  text = text.replace(/^[a-z*]+:\/\//, "").split("#")[0].trim().replace(/\*{2,}/g, "*");
  const slash = text.indexOf("/");
  let host = slash < 0 ? text : text.slice(0, slash);
  let path = slash < 0 ? "" : text.slice(slash);
  if (host.startsWith("www.")) host = host.slice(4);
  if (!host && path) host = "*";
  if (!/^[a-z0-9.*-]+$/.test(host) || (!host.includes(".") && !host.includes("*")) || path.includes(" ")) return null;
  if (path === "/" || path === "/*") path = "";
  return host + path;
}
// ------------------------------------------------------- website and app limits
//
// Time budgets and blocked hours that apply all day (the Limits tab), for
// websites (the extension enforces them) and Windows apps (buZyist closes the
// app, or only beeps). Rules are edited in memory and saved with the rest;
// buZyist checks them (clean_site_limit, clean_app_limit) and keeps the
// totals. One editor is open at a time.
const LIMITS = {
  site: { rules: [], usage: {}, list: "limitList", editor: "limitEditor", add: "addLimitBtn",
          fields: "limitsFields", on: "s_site_limits_on", usageCall: "site_usage" },
  app: { rules: [], usage: {}, list: "appLimitList", editor: "appLimitEditor", add: "addAppLimitBtn",
         fields: "appLimitsFields", on: "s_app_limits_on", usageCall: "app_usage" },
};
let limitDraft = null;    // {kind, index, rule} while an editor is open
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const unitPlural = (every, unit) => (every === 1 ? unit : `${every} ${unit}s`);
const budgetText = (b) => `${b.minutes} min per ${unitPlural(b.every, b.unit)}`
  + (!b.days || b.days.length === 7 ? "" : ", " + daysText(b.days));
const clockText = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const appLabel = (exe) => exe.replace(/\.exe$/i, "").split(".")[0] || exe;  // exe_label in blocker.py
const limitNames = (kind, rule) => (kind === "site" ? rule.patterns : [...new Set(rule.apps.map(appLabel))]).join(", ");
const REMIND_MIN = 1, REMIND_MAX = 120;  // APP_LIMIT_REMIND_MIN/MAX in buzyist/constants.py
const beeps = (kind, rule) => kind === "app" && rule.action === "beep";

function daysText(days) {
  if (days.length === 7) return "every day";
  if (days.join() === "0,1,2,3,4") return "Mon–Fri";
  if (days.join() === "5,6") return "Sat–Sun";
  return days.map((d) => DAY_NAMES[d]).join(", ");
}

// One kind's list ("site" or "app"), or both without one.
function renderLimits(kind) {
  if (!kind) { renderLimits("site"); renderLimits("app"); return; }
  const k = LIMITS[kind];
  const list = $(k.list);
  list.replaceChildren();
  k.rules.forEach((rule, i) => {
    const status = k.usage[rule.id];
    const row = el("div", "limit-row" + (rule.enabled ? "" : " off"));
    const head = el("div", "limit-head");
    const sw = el("label", "switch");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = rule.enabled;
    box.onchange = () => { rule.enabled = box.checked; renderLimits(kind); };
    sw.append(box, el("span", "knob"));
    sw.title = rule.enabled ? "Turn this limit off" : "Turn this limit on";
    const edit = el("button", "btn sm ghost", "Edit");
    edit.type = "button";
    edit.onclick = () => openLimitEditor(kind, i);
    const x = el("button", "remove", "×");
    x.type = "button";
    x.title = "Remove this limit";
    x.onclick = () => { k.rules.splice(i, 1); renderLimits(kind); };
    const name = el("span", "site", limitNames(kind, rule));
    if (kind === "app") name.title = rule.apps.join(", ");
    head.append(sw, name, edit, x);
    row.append(head);
    if (kind === "app") {
      row.append(el("div", "limit-line", rule.action === "beep"
        ? `Beeps when reached, again every ${rule.remind || 5} min; the app stays open`
        : "Closes the app when reached"));
    }
    if (status && status.blocked) {
      const what = beeps(kind, rule) ? "Over the limit" : kind === "app" ? "Closed" : "Blocked";
      row.append(el("div", "limit-tag", `${what} until ${clockText(status.until_ms)}`));
    }
    rule.budgets.forEach((b, j) => {
      const line = el("div", "limit-line");
      const live = status && status.budgets && status.budgets[j];
      if (live && live.budget_s === b.minutes * 60) {
        const used = Math.min(live.used_s, live.budget_s);
        const text = el("div");
        text.append(el("b", "", budgetText(b)),
          live.active ? ` · ${Math.floor(used / 60)} of ${b.minutes} min used` : " · not today");
        const bar = el("div", "limit-bar" + (live.remaining_s <= 0 ? " full" : ""));
        const fill = el("i");
        fill.style.width = Math.round((100 * used) / live.budget_s) + "%";
        bar.append(fill);
        line.append(text, bar);
      } else {
        line.append(el("div", "", budgetText(b)));
      }
      row.append(line);
    });
    for (const w of rule.windows) {
      const line = el("div", "limit-line");
      line.append(el("div", "", `${beeps(kind, rule) ? "Beeps" : "Blocked"} ${daysText(w.days)}, ${w.from}–${w.to}`));
      row.append(line);
    }
    list.append(row);
  });
  $(k.fields).classList.toggle("muted", !$(k.on).checked);
  renderTabDots();
}

async function refreshLimitUsage(kind) {
  if (!kind) { refreshLimitUsage("site"); refreshLimitUsage("app"); return; }
  const k = LIMITS[kind];
  const r = await api[k.usageCall]();
  if (!r.ok) return;
  k.usage = Object.fromEntries(r.rules.map((rule) => [rule.id, rule]));
  renderLimits(kind);
}
setInterval(async () => {
  if (!$("drawer").classList.contains("on") || settingsTab !== "limits") return;
  refreshLimitUsage();
  const r = await api.sync_info();
  if (r.ok) renderSync(r);
}, 5000);

// Sharing limits with other PCs through Chrome sync: one browser profile per
// PC does it (buzyist/app_sync.py, "sharing across PCs"). The profiles come from the
// extensions that have called buZyist; the first one is picked by itself.
let syncOwner = "";  // as picked here, saved with the rest

const agoText = (ms) => {
  const min = Math.round((Date.now() - ms) / 60000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  if (min < 48 * 60) return `${Math.round(min / 60)} h ago`;
  return `${Math.round(min / 1440)} days ago`;
};

function renderSync(info) {
  info = info || { instances: [], devices: [] };
  const select = $("s_sync_owner");
  if (document.activeElement !== select) {
    select.replaceChildren();
    const auto = el("option", "", info.instances.length ? "The first profile to connect" : "Waiting for the extension\u2026");
    auto.value = "";
    select.append(auto);
    for (const inst of info.instances) {
      const name = inst.email || `A profile not signed in to Chrome (${inst.id.slice(0, 4)})`;
      const opt = el("option", "", name + (inst.connected ? "" : ` \u00b7 last seen ${agoText(inst.seen_ms)}`));
      opt.value = inst.id;
      select.append(opt);
    }
    if (syncOwner && !info.instances.some((i) => i.id === syncOwner)) {
      const gone = el("option", "", "A profile not seen lately");
      gone.value = syncOwner;
      select.append(gone);
    }
    select.value = syncOwner;
  }
  const owner = info.instances.find((i) => i.id === syncOwner);
  const status = $("syncStatus");
  const others = info.devices.map((d) => `${d.name} (${agoText(d.t)})`).join(", ");
  const synced = !!info.last_sync_ms;
  let text;
  if (!owner) text = "Not shared yet: load the extension in Chrome";
  else if (!owner.email) text = "This profile isn't signed in to Chrome, so nothing leaves this PC";
  else if (!synced) text = "Waiting for Chrome sync\u2026";
  else text = `Synced ${agoText(info.last_sync_ms)}` + (others ? ` \u00b7 also counting ${others}` : " \u00b7 no other PC yet");
  status.textContent = text;
  const good = !!(owner && owner.email && synced);
  status.classList.toggle("ok", good);
  status.classList.toggle("bad", !good);
  syncMuted();
}
const syncMuted = () => $("syncFields").classList.toggle("muted", !$("s_sync_limits").checked && !$("s_sync_session").checked);
$("s_sync_owner").onchange = () => { syncOwner = $("s_sync_owner").value; };
$("s_sync_limits").onchange = $("s_sync_session").onchange = syncMuted;

let namesText = "";  // what's typed in the editor's websites (or apps) box

function openLimitEditor(kind, index) {
  closeLimitEditor();
  const fresh = kind === "site"
    ? { patterns: [], budgets: [{ minutes: 5, every: 1, unit: "hour", days: [0, 1, 2, 3, 4, 5, 6] }] }
    : { apps: [], action: "close", remind: 5, budgets: [{ minutes: 30, every: 1, unit: "day", days: [0, 1, 2, 3, 4, 5, 6] }] };
  const rule = index === undefined
    ? { enabled: true, windows: [], ...fresh }
    : JSON.parse(JSON.stringify(LIMITS[kind].rules[index]));
  limitDraft = { kind, index, rule };
  namesText = (kind === "site" ? rule.patterns : rule.apps).join(", ");
  renderLimitEditor();
}

function closeLimitEditor() {
  limitDraft = null;
  for (const k of Object.values(LIMITS)) {
    $(k.editor).classList.add("hidden");
    $(k.editor).replaceChildren();
    $(k.add).classList.remove("hidden");
  }
}

function numberInput(value, min, onchange) {
  const input = el("input", "input");
  input.type = "number";
  input.min = min;
  input.value = value;
  input.oninput = () => onchange(parseInt(input.value, 10) || 0);
  return input;
}

function removeButton(onclick, title) {
  const x = el("button", "remove", "×");
  x.type = "button";
  x.title = title;
  x.onclick = onclick;
  return x;
}

// Seven day toggles editing item.days (a time limit or blocked hours).
function dayButtons(item) {
  if (!item.days) item.days = [0, 1, 2, 3, 4, 5, 6];
  const wrap = el("div", "limit-days");
  DAY_NAMES.forEach((name, d) => {
    const b = el("button", "day", name);
    b.type = "button";
    b.setAttribute("aria-pressed", item.days.includes(d));
    b.onclick = () => {
      item.days = item.days.includes(d) ? item.days.filter((x) => x !== d) : [...item.days, d].sort();
      b.setAttribute("aria-pressed", item.days.includes(d));
    };
    wrap.append(b);
  });
  return wrap;
}

function renderLimitEditor() {
  const { kind, rule } = limitDraft;
  const k = LIMITS[kind];
  const box = $(k.editor);
  box.replaceChildren();
  box.classList.remove("hidden");
  $(k.add).classList.add("hidden");

  const names = el("input", "input");
  names.id = kind === "site" ? "limitSite" : "limitApps";
  names.spellcheck = false;
  names.placeholder = kind === "site" ? "Websites, e.g. instagram.com, facebook.com" : "Apps, e.g. WhatsApp.exe, Slack.exe";
  names.value = namesText;
  names.oninput = () => { namesText = names.value; };
  box.append(names, el("div", "help", kind === "site"
    ? "Several websites can share one limit: the time adds up across all of them."
    : "Program names, as Task Manager's Details tab shows them, separated by commas. Several apps can share one limit: the time adds up across all of them."));
  box.onkeydown = (e) => {
    if (e.key === "Enter" && e.target.tagName !== "BUTTON" && e.target.tagName !== "SELECT") {
      e.preventDefault(); e.stopPropagation(); commitLimitDraft();
    }
  };

  const hoursHead = el("h4");
  const addWindow = el("button", "btn sm");
  const hoursText = () => {
    hoursHead.textContent = beeps(kind, rule) ? "Hours it beeps in" : "Blocked hours";
    addWindow.textContent = beeps(kind, rule) ? "Add hours" : "Add blocked hours";
  };
  if (kind === "app") {
    const row = el("div", "limit-sub");
    const action = el("select", "input");
    for (const [value, text] of [["close", "Close the app"], ["beep", "Beep, keep it open"]]) {
      const o = el("option", "", text);
      o.value = value;
      action.append(o);
    }
    action.value = rule.action;
    if (!rule.remind) rule.remind = 5;
    const remind = el("span", "limit-sub");
    remind.append("again every", numberInput(rule.remind, REMIND_MIN, (n) => { rule.remind = n; }), "min");
    remind.classList.toggle("hidden", rule.action !== "beep");
    action.onchange = () => {
      rule.action = action.value;
      remind.classList.toggle("hidden", rule.action !== "beep");
      hoursText();
    };
    row.append("When a limit is reached:", action, remind);
    box.append(row);
  }

  box.append(el("h4", "", "Time allowed"));
  rule.budgets.forEach((b, i) => {
    const row = el("div", "limit-sub");
    row.append(dayButtons(b));
    const amount = el("div", "limit-sub");
    const unit = el("select", "input");
    for (const u of ["minute", "hour", "day", "week"]) {
      const o = el("option", "", u + "s");
      o.value = u;
      o.selected = u === b.unit;
      unit.append(o);
    }
    unit.onchange = () => { b.unit = unit.value; };
    amount.append(numberInput(b.minutes, 1, (n) => { b.minutes = n; }), "min every",
                  numberInput(b.every, 1, (n) => { b.every = n; }), unit,
                  removeButton(() => { rule.budgets.splice(i, 1); renderLimitEditor(); }, "Remove this limit"));
    const group = el("div", "limit-group");
    group.append(amount, row);
    box.append(group);
  });
  const addBudget = el("button", "btn sm", "Add a time limit");
  addBudget.type = "button";
  addBudget.onclick = () => { rule.budgets.push({ minutes: 30, every: 1, unit: "day", days: [0, 1, 2, 3, 4, 5, 6] }); renderLimitEditor(); };
  box.append(addBudget);

  box.append(hoursHead);
  rule.windows.forEach((w, i) => {
    const row = el("div", "limit-sub");
    row.append(dayButtons(w));
    const from = el("input", "input");
    from.type = "time";
    from.value = w.from;
    from.oninput = () => { w.from = from.value; };
    const to = el("input", "input");
    to.type = "time";
    to.value = w.to;
    to.oninput = () => { w.to = to.value; };
    row.append(from, "to", to, removeButton(() => { rule.windows.splice(i, 1); renderLimitEditor(); }, "Remove these hours"));
    box.append(row);
  });
  addWindow.type = "button";
  addWindow.onclick = () => { rule.windows.push({ days: [0, 1, 2, 3, 4], from: "08:00", to: "17:00" }); renderLimitEditor(); };
  hoursText();
  box.append(addWindow);
  box.append(el("div", "help", "Hours ending before they start run past midnight (22:00 to 06:00). 00:00 to 00:00 is the whole day."));

  const actions = el("div", "limit-actions");
  const cancel = el("button", "btn ghost", "Cancel");
  cancel.type = "button";
  cancel.onclick = closeLimitEditor;
  const done = el("button", "btn primary", limitDraft.index === undefined ? "Add limit" : "Done");
  done.type = "button";
  done.onclick = commitLimitDraft;
  actions.append(cancel, done);
  box.append(actions);
  names.focus();
}

// The apps typed in the editor as program names (clean_app_limit); null (with a toast) if one can't be.
function draftApps(action) {
  const key = (n) => n.toLowerCase().replace(/\.exe$/, "");
  const apps = [];
  for (const part of namesText.split(/[,;\n]+/).map((p) => p.trim().replace(/^"|"$/g, "")).filter(Boolean)) {
    if (/[\\/]/.test(part)) { toast("App names are just the program, like WhatsApp.exe.", true); return null; }
    const exe = /\.exe$/i.test(part) ? part : part + ".exe";
    if (action === "close" && PROTECTED_EXES.has(key(exe))) {
      toast(`buZyist won't close ${exe}: Windows or your browser needs it. Make that limit beep.`, true);
      return null;
    }
    if (!apps.some((a) => key(a) === key(exe))) apps.push(exe);
  }
  if (!apps.length) { toast("Type an app like WhatsApp.exe.", true); return null; }
  if (apps.length > 20) { toast("Keep a limit to 20 apps or fewer.", true); return null; }
  return apps;
}

// The websites typed in the editor as patterns; null (with a toast) if one isn't a website.
function draftSites() {
  const patterns = [];
  for (const part of namesText.split(/[\s,;]+/).filter(Boolean)) {
    const pattern = cleanSitePattern(part);
    if (pattern === null) { toast(`"${part}" isn't a website like instagram.com or *.reddit.com.`, true); return null; }
    if (pattern && !patterns.includes(pattern)) patterns.push(pattern);
  }
  if (!patterns.length) { toast("Type a website like instagram.com or *.reddit.com.", true); return null; }
  if (patterns.length > 20) { toast("Keep a limit to 20 websites or fewer.", true); return null; }
  return patterns;
}

// Move the editor's rule into its list. False (with a toast) if it isn't usable yet.
function commitLimitDraft() {
  const { kind, index, rule } = limitDraft;
  const k = LIMITS[kind];
  const names = kind === "site" ? draftSites() : draftApps(rule.action);
  if (!names) return false;
  if (beeps(kind, rule) && !(rule.remind >= REMIND_MIN && rule.remind <= REMIND_MAX)) {
    toast(`Beep again every ${REMIND_MIN} to ${REMIND_MAX} minutes.`, true);
    return false;
  }
  if (!rule.budgets.length && !rule.windows.length) { toast("Add a time limit or blocked hours.", true); return false; }
  if (rule.budgets.some((b) => !b.days.length)) { toast("Pick at least one day for each time limit.", true); return false; }
  if (rule.budgets.some((b) => b.minutes < 1 || b.every < 1)) { toast("Time limits need at least 1 minute and a period of at least 1.", true); return false; }
  if (rule.windows.some((w) => !w.days.length || !w.from || !w.to)) { toast("Blocked hours need days and times.", true); return false; }
  if (k.rules.length >= 30 && index === undefined) {
    toast(`Keep the ${kind === "site" ? "website" : "app"} limits under 30.`, true);
    return false;
  }
  if (kind === "site") rule.patterns = names;
  else rule.apps = names;
  if (index === undefined) k.rules.push(rule);
  else k.rules[index] = rule;
  closeLimitEditor();
  renderLimits(kind);
  return true;
}

$("addLimitBtn").onclick = () => openLimitEditor("site");
$("addAppLimitBtn").onclick = () => openLimitEditor("app");
$("s_site_limits_on").onchange = () => renderLimits("site");
$("s_app_limits_on").onchange = () => renderLimits("app");

for (const r of document.querySelectorAll('input[name="site_mode"]')) r.onchange = () => renderBlockedSites();
$("addBlockedSiteBtn").onclick = addBlockedSite;
$("addBlockedSite").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); addBlockedSite(); }
});
$("s_block_sites").onchange = () => renderBlockedSites();
$("extChip").onclick = () => openSettings("sites");
$("openExtensionsPage").onclick = () => $("installExtension").click();
$("openExtensionFolder").onclick = () => api.open_extension_folder();
$("copyExtensionPath").onclick = async () => {
  const r = await api.copy_extension_path();
  toast(r.ok ? "Folder path copied" : "Couldn't reach the clipboard; copy the path by hand.", !r.ok);
};
$("installExtension").onclick = async () => {
  const r = await api.install_extension();
  if (!r.ok) return toast(r.error, true);
  const copied = r.copied ? " The folder path is on your clipboard." : "";
  if (r.browser) toast(`Opened ${r.browser}'s extensions page.${copied}`);
  else toast(`Couldn't find Chrome. Open chrome://extensions in your browser.${copied}`, true);
};

function closeSettings() {
  $("drawer").classList.remove("on");
  if (!(state && state.ended)) $("scrim").classList.remove("on");
}

function formValues(names) {
  const out = {};
  for (const input of $("settingsForm").querySelectorAll("input[name]")) {
    if (names && !names.includes(input.name)) continue;
    if (input.type === "radio") { if (input.checked) out[input.name] = input.value; continue; }
    out[input.name] = input.type === "checkbox" ? input.checked : input.value;
  }
  return out;
}

$("openSettings").onclick = () => openSettings();
$("openData").onclick = () => api.open_data_folder();
$("openRepo").onclick = () => api.open_repo();
$("closeSettings").onclick = closeSettings;
$("updateChip").onclick = async () => {
  await openSettings("about");
};
$("checkUpdate").onclick = async () => {
  const out = $("updateResult");
  out.className = "test-result";
  out.textContent = "Checking GitHub…";
  $("checkUpdate").disabled = true;
  const result = await api.check_update();
  $("checkUpdate").disabled = false;
  if (!result.ok) { out.className = "test-result bad"; out.textContent = result.error; return; }
  await poll();
  if (!result.update) { out.className = "test-result ok"; out.textContent = "You have the latest version."; }
};
$("installUpdate").onclick = async () => {
  const u = state && state.update;
  if (!u) return;
  if (!u.installable) return api.open_url(u.url);
  const where = state.bar.local ? "" : " on the bar";
  if (state.session && !confirm(`buZyist restarts to update. The session keeps running${where} and picks up again after the restart. Update now?`)) return;
  $("installUpdate").disabled = true;
  $("installUpdate").textContent = "Installing…";
  const result = await api.install_update();
  if (!result.ok) { await poll(); return toast(result.error, true); }
  toast(`Installing buZyist ${u.version}. It restarts by itself.`);
};
$("releaseNotes").onclick = () => state && state.update && api.open_url(state.update.url);
$("cancelSettings").onclick = closeSettings;
$("scrim").onclick = () => {
  if (editing !== undefined) closeFilterEditor();
  else if ($("drawer").classList.contains("on")) closeSettings();
};
$("revealToken").onclick = () => {
  const i = $("s_todoist_token");
  i.type = i.type === "password" ? "text" : "password";
};
$("settingsForm").onsubmit = (e) => { e.preventDefault(); $("saveSettings").click(); };
$("saveSettings").onclick = async () => {
  addBlockedApp(); // a name typed but not yet added still counts
  addBlockedSite(); // same for a website typed but not yet added
  if (limitDraft && !commitLimitDraft()) return; // a limit being edited counts too
  const result = await api.save_settings({ ...formValues(), ...secretClears(), blocked_apps: blockedApps, blocked_sites: siteLists.block, allowed_sites: siteLists.allow, site_limits: LIMITS.site.rules, app_limits: LIMITS.app.rules, sync_owner: syncOwner });
  if (!result.ok) return toast(result.error, true);
  closeSettings();
  toast("Settings saved");
  await poll();
  loadTasks();
};
$("testBar").onclick = async () => {
  const out = $("testResult");
  out.className = "test-result";
  out.textContent = "Testing…";
  const { clear_busybar_pin } = secretClears();
  const saved = await api.save_settings({ ...formValues(["use_busybar", "busybar_ip", "busybar_pin", "usb_fallback"]), clear_busybar_pin });
  if (!saved.ok) { out.className = "test-result bad"; out.textContent = saved.error; return; }
  const result = await api.test_bar();
  out.className = "test-result " + (result.ok ? "ok" : "bad");
  out.textContent = result.ok ? `Connected over ${result.via}${result.version ? " · firmware " + result.version : ""}` : result.error;
};

// ------------------------------------------------------------- keyboard

document.addEventListener("keydown", (e) => {
  const inDrawer = $("drawer").classList.contains("on");
  const inEditor = editing !== undefined;
  const menu = $("taskMenu");
  if (menu) {
    const items = [...menu.querySelectorAll("button")];
    const at = items.indexOf(document.activeElement);
    if (e.key === "Escape") closeTaskMenu();
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      items[(at + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
    } else if (e.key === "Enter" && at >= 0) items[at].click();
    else if (e.key !== "Tab") return;
    e.preventDefault();
    return;
  }
  if (e.key === "Escape") {
    if (inEditor) return closeFilterEditor();
    if (inDrawer) return closeSettings();
    if (state && state.ended) return $("endedClose").click();
    if ($("search").value) { $("search").value = ""; renderTasks(); return; }
    api.hide_main();
    return;
  }
  if (inEditor || inDrawer || (state && state.ended)) return;
  if (e.ctrlKey && !e.altKey && /^[1-9]$/.test(e.key)) {
    e.preventDefault();
    const f = filters[+e.key - 1];
    if (f) selectFilter(f.name);
    return;
  }
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    sel = Math.max(0, Math.min(shown.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
    markSelection(true);
  } else if (e.key === "Enter" && shown[sel] && !e.target.closest(".stepper")) {
    e.preventDefault();
    startTask(shown[sel], $("list").querySelectorAll(".task")[sel]);
  } else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey && e.target.tagName !== "INPUT") {
    $("search").focus();
  }
});
$("search").addEventListener("input", () => { sel = 0; renderTasks(); });
$("refresh").onclick = () => loadTasks();

// ------------------------------------------------------------- startup

// Polling: every second while the window is on screen, every 15 s while it
// sits in the tray. Python says which through App.setVisible on every hide
// and show (it hides windows with SW_HIDE, which document.hidden doesn't
// always notice), and onShown means visible too.
let visible = true;
const onScreen = () => visible && !document.hidden;

window.App = {
  setVisible(v) {
    visible = !!v;
    if (visible) poll();
  },
  onShown(focusSearch) {
    visible = true;
    if (Date.now() - lastTaskLoad > 30000) loadTasks(true);
    poll();
    // A window that was hidden in the tray can still be throttled for a
    // moment after it shows; poll again so the end-of-session prompt
    // doesn't wait for a click.
    setTimeout(poll, 200);
    setTimeout(poll, 800);
    if (focusSearch) {
      $("search").focus();
      $("search").select();
    }
  },
};

// Re-sync as soon as the window is restored or focused.
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
window.addEventListener("focus", () => { visible = true; poll(); });  // focused means on screen
window.addEventListener("resize", () => poll());

async function boot() {
  api = window.pywebview.api;
  await poll();
  if (state.needs_setup) {
    showEmpty("Welcome to buZyist", "Add your Todoist API token to see your tasks, or turn Todoist off in Settings to run sessions without tasks.", true);
    tasksWereOff = true;
    openSettings();
  } else if (state.todoist) {
    loadTasks();
  }
  setInterval(() => { if (onScreen()) poll(); }, 1000);
  setInterval(() => { if (!onScreen()) poll(); }, 15000);  // hidden: a slow safety net
  setInterval(() => { if (onScreen()) renderTimer(); }, 250);
  // keep the list fresh while the window stays open
  setInterval(() => { if (!document.hidden && Date.now() - lastTaskLoad > 300000) loadTasks(true); }, 60000);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot, { once: true });
