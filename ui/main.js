"use strict";

const $ = (id) => document.getElementById(id);
const RING = 2 * Math.PI * 104;
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

  // ring + time
  let left, total;
  if (t) {
    left = Math.max(0, timeLeft());
    total = t.total_ms;
  } else {
    left = total = (pomo ? pomo.work_minutes : state.pomodoro.work_minutes) * 60000;
  }
  $("time").textContent = clock(left);
  $("ringProgress").style.strokeDasharray = RING;
  $("ringProgress").style.strokeDashoffset = t ? RING * (1 - left / total) : RING;
  $("phaseTag").textContent = { idle: "Ready", paused: "Paused", work: "Focus", rest: "Break" }[phase];

  // round dots
  const rounds = t ? t.rounds : (pomo || state.pomodoro).cycles;
  const dots = $("rounds");
  if (dots.childElementCount !== rounds) {
    dots.textContent = "";
    for (let i = 0; i < Math.min(rounds, 12); i++) dots.append(el("i"));
  }
  [...dots.children].forEach((dot, i) => {
    const n = i + 1;
    dot.className = !t ? "" : n < t.round || (n === t.round && t.phase === "rest") ? "done" : n === t.round ? "now" : "";
  });
  document.title = t ? `${clock(left)} · ${s ? s.task.content : "Busyist"}` : "Busyist";
}

function renderCurrent() {
  const t = state.timer, s = state.session;
  const box = $("current");
  const meta = $("curMeta");
  meta.textContent = "";
  box.classList.toggle("idle", !s);
  $("controls").classList.toggle("hidden", !t);
  if (s) {
    const phase = t && t.phase === "rest" ? "On a break" : "Focusing";
    $("curEyebrow").textContent = t ? `${phase} · round ${t.round} of ${t.rounds} · since ${s.started_at}` : "Wrapping up";
    $("curTask").textContent = s.task.content;
    if (s.task.project) {
      const proj = el("span", "", s.task.project);
      proj.style.color = s.task.color;
      meta.append(proj);
    }
    if (s.label_added && state.label) meta.append(el("span", "", "@" + state.label));
    if (s.task.url) {
      const open = el("button", "link", "Open in Todoist");
      open.onclick = () => api.open_url(s.task.url);
      meta.append(open);
    }
  } else if (t) {
    $("curEyebrow").textContent = state.bar.local ? "Timer running" : "Running on the bar";
    $("curTask").textContent = state.bar.local ? "No task linked" : "Started on the bar, no task linked";
    meta.append(el("span", "", "Pick a task to replace it"));
  } else {
    $("curEyebrow").textContent = "Nothing in focus";
    $("curTask").textContent = state.todoist ? "Pick a task to start a session" : "Start a pomodoro session";
  }
  $("freeBox").classList.toggle("hidden", !!t || !!s);
  if (t) {
    $("pauseIcon").setAttribute("href", t.paused ? "#i-play" : "#i-pause");
    $("btnPause").title = t.paused ? "Resume" : "Pause";
  }
  $("setupHint").textContent = t ? "Changes apply to the next session" : "Used for the next session";
}

function renderChrome() {
  const today = state.today;
  const chip = $("today");
  chip.textContent = "";
  chip.append("🍅 ", el("b", "", String(today.pomodoros)), today.pomodoros === 1 ? " pomodoro today" : " pomodoros today");
  if (today.minutes) chip.append(" · " + duration(today.minutes));
  const done = $("completed");
  done.textContent = "";
  done.append("✓ ", el("b", "", String(today.completed)), " completed");

  const bar = state.bar;
  $("barChip").className = "chip" + (bar.ok === true ? " ok" : bar.ok === false ? " bad" : "");
  $("barText").textContent = bar.local ? "Timer on this PC"
    : bar.ok === true ? `BUSY Bar · ${bar.via}` : bar.ok === false ? "Bar offline" : "Looking for the bar";
  $("barChip").title = bar.ok === false ? bar.error : "";
  $("hotkeyHint").textContent = state.hotkey ? state.hotkey.replace(/\b\w/g, (c) => c.toUpperCase()) : "";

  const u = state.update;
  $("updateChip").classList.toggle("hidden", !u);
  if (u) $("updateChip").textContent = u.busy ? "Updating…" : `Update ${u.version}`;
  if ($("drawer").classList.contains("on")) {
    renderUpdate();
    renderExtension(!!state.extension_connected);
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
  out.textContent = u.error || (u.busy ? "Downloading the update…" : `Busyist ${u.version} is available. Installing it restarts Busyist.`);
}

function renderEnded() {
  const e = state.ended;
  $("endedWrap").classList.toggle("on", !!e);
  $("scrim").classList.toggle("on", !!e || $("drawer").classList.contains("on") || editing !== undefined);
  if (!e) return;
  $("endedTomatoes").textContent = e.done ? (e.done > 8 ? "🍅 × " + e.done : "🍅".repeat(e.done)) : "⏹";
  $("endedTitle").textContent = e.reason === "finished" ? "Session complete" : "Session stopped";
  $("endedTask").textContent = e.task.content;
  $("endedComplete").classList.toggle("hidden", !e.task.id || !state.todoist);
  $("endedSummary").textContent = e.done
    ? `${e.done} pomodoro${e.done === 1 ? "" : "s"} · ${duration(e.minutes)} of focus`
    : "No full pomodoro this time.";
  $("endedProblems").textContent = (e.problems || []).join(" ");
}

async function poll() {
  try {
    state = await api.get_state();
    document.documentElement.dataset.theme = state.theme || "auto";
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
  if (!tasks.length) return showEmpty("All clear", "Nothing matches your filter. Enjoy it.");
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
    if (meta.childElementCount) body.append(meta);
    const play = el("button", "play");
    play.append(icon("play"), "Start");
    play.tabIndex = -1;
    const check = el("button", "prio " + prioClass(t.priority));
    check.title = "Mark as done";
    check.tabIndex = -1;
    check.append(icon("check"));
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

function markCurrentTask() {
  const id = state && state.session && state.session.task.id;
  for (const li of $("list").querySelectorAll(".task")) {
    const on = li.dataset.id === id;
    let badge = li.querySelector(".badge");
    if (on && !badge) {
      badge = el("span", "badge", "In focus");
      li.insertBefore(badge, li.querySelector(".play"));
    } else if (!on && badge) {
      badge.remove();
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
  toast(`Started${state && state.bar.local ? "" : " on the bar"} · ${pomo.work_minutes} min focus`);
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
  toast("Completed in Todoist ✓");
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
  toast(`Started${state && state.bar.local ? "" : " on the bar"} · ${pomo.work_minutes} min focus`);
  $("freeName").value = "";
  poll();
}
$("startFree").onclick = () => startFree();
$("freeName").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.stopPropagation(); startFree(); } });
$("btnStop").onclick = () => control("stop");
$("btnSkip").onclick = () => control("skip");
$("btnPause").onclick = () => control(state.timer && state.timer.paused ? "resume" : "pause");

$("endedClose").onclick = async () => { await api.dismiss_ended(); poll(); };
$("endedComplete").onclick = async () => {
  const e = state.ended;
  const result = await api.complete(e.task.id);
  if (!result.ok) return toast(result.error, true);
  toast("Completed in Todoist ✓");
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
  $("sitesDot").classList.toggle("hidden", !sitesOn || !!(state && state.extension_connected));
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
  blockedApps = (s.blocked_apps || []).map((a) => ({ ...a, exes: [...a.exes] }));
  $("addBlockedApp").value = "";
  renderBlockedApps();
  siteLists = { block: [...(s.blocked_sites || [])], allow: [...(s.allowed_sites || [])] };
  $("addBlockedSite").value = "";
  $("extDir").textContent = s.extension_dir || "extension";
  $("extDir").title = s.extension_dir || "";
  for (const n of document.querySelectorAll(".browser-name")) n.textContent = s.extension_browser || "Chrome";
  renderBlockedSites();
  renderExtension(s.extension_connected);
  showBarFields();
  showTodoistFields();
  $("aboutVersion").textContent = "Busyist " + s.version;
  $("aboutData").textContent = s.data_dir;
  $("autoUpdateRow").classList.toggle("hidden", !s.can_auto_update);
  $("updateResult").textContent = "";
  renderUpdate();
  if (s.use_todoist && !s.todoist_token) tab = "todoist";
  showTab(typeof tab === "string" ? tab : settingsTab);
  $("drawer").classList.add("on");
  $("scrim").classList.add("on");
  setTimeout(() => {
    if (s.use_todoist && !s.todoist_token) $("s_todoist_token").focus();
    else document.querySelector("#settingsTabs .stab.on").focus();
  }, 220);
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

function addBlockedApp() {
  const input = $("addBlockedApp");
  const name = input.value.trim();
  if (!name) return;
  if (/[\\/]/.test(name)) return toast("App names are just the program, like WhatsApp.exe.", true);
  const key = (n) => n.toLowerCase().replace(/\.exe$/, "");
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
// extension reads them from Busyist. Edited here, saved with the rest.
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

// The setup steps show until the extension has called Busyist.
function renderExtension(connected) {
  const status = $("extStatus");
  status.textContent = connected ? "Browser extension: connected" : "Browser extension: not connected";
  status.classList.toggle("ok", connected);
  status.classList.toggle("bad", !connected);
  $("extSetup").classList.toggle("hidden", connected);
  $("extDone").classList.toggle("hidden", !connected);
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
// Mirrors clean_site_pattern in busyist.py, which has the final say on save.
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
for (const r of document.querySelectorAll('input[name="site_mode"]')) r.onchange = () => renderBlockedSites();
$("addBlockedSiteBtn").onclick = addBlockedSite;
$("addBlockedSite").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); addBlockedSite(); }
});
$("s_block_sites").onchange = () => renderBlockedSites();
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
  if (state.session && !confirm(`Busyist restarts to update. The session keeps running${where} and picks up again after the restart. Update now?`)) return;
  $("installUpdate").disabled = true;
  $("installUpdate").textContent = "Installing…";
  const result = await api.install_update();
  if (!result.ok) { await poll(); return toast(result.error, true); }
  toast(`Installing Busyist ${u.version}. It restarts by itself.`);
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
  const result = await api.save_settings({ ...formValues(), blocked_apps: blockedApps, blocked_sites: siteLists.block, allowed_sites: siteLists.allow });
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
  const saved = await api.save_settings(formValues(["use_busybar", "busybar_ip", "busybar_pin", "usb_fallback"]));
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

window.App = {
  onShown(focusSearch) {
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
window.addEventListener("focus", () => poll());
window.addEventListener("resize", () => poll());

async function boot() {
  api = window.pywebview.api;
  await poll();
  if (state.needs_setup) {
    showEmpty("Welcome to Busyist", "Add your Todoist API token to see your tasks, or turn Todoist off in Settings to run sessions without tasks.", true);
    tasksWereOff = true;
    openSettings();
  } else if (state.todoist) {
    loadTasks();
  }
  setInterval(poll, 1000);
  setInterval(renderTimer, 250);
  // keep the list fresh while the window stays open
  setInterval(() => { if (!document.hidden && Date.now() - lastTaskLoad > 300000) loadTasks(true); }, 60000);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot, { once: true });
