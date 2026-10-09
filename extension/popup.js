// The toolbar popup: what Busyist is doing, and where each website limit
// stands. Everything comes from the background worker (popupData), which also
// works while Busyist is closed, from the last answer it got.

const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const weekday = (ms) => new Date(ms).toLocaleDateString([], { weekday: "short" });
const sameDay = (a, b) => new Date(a).toDateString() === new Date(b).toDateString();
// "17:00", or "Thu 08:00" when it isn't today.
const when = (ms) => (sameDay(ms, Date.now()) ? "" : weekday(ms) + " ") + clock(ms);

function duration(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  if (s < 60) return `${s} sec`;
  const m = Math.ceil(s / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

const names = (patterns) => patterns.join(", ");

// A bar showing how much of a budget is left.
function bar(left, budget) {
  const wrap = el("div", "bar" + (left <= 0 ? " full" : left <= 120 ? " warn" : ""));
  const fill = el("i");
  fill.style.width = Math.max(0, Math.min(100, (100 * left) / budget)) + "%";
  wrap.append(fill);
  return wrap;
}

function renderFocus(data) {
  const box = $("focus");
  box.replaceChildren();
  const f = data.focus;
  if (f.active) {
    const mins = Math.max(0, Math.ceil((f.ends_at_ms - Date.now()) / 60000));
    box.append(el("div", "big bad", `${mins} min left`));
    box.append(el("div", "small", f.task ? `Focusing on ${f.task}` : "Work phase running"));
    box.append(el("div", "small muted", f.mode === "allow"
      ? "Only your allowed websites open until it ends."
      : f.sites ? `${f.sites} website${f.sites === 1 ? " is" : "s are"} blocked until it ends.` : "No websites are blocked during focus."));
  } else if (data.offline && !data.known) {
    box.append(el("div", "small muted", "Unknown: Busyist hasn't answered yet."));
  } else if (data.offline) {
    box.append(el("div", "small muted", "Unknown while Busyist is closed."));
  } else {
    box.append(el("div", "small", "No work phase running."));
    box.append(el("div", "small muted", "Start one in Busyist to block distracting websites."));
  }
}

function ruleRow(rule) {
  const row = el("div", "rule");
  const head = el("div", "name");
  head.append(names(rule.patterns));
  if (rule.here) head.append(el("span", "tag", "this tab"));
  row.append(head);

  if (rule.state) {
    row.append(el("div", "line blocked", rule.state.reason === "window"
      ? `Blocked until ${when(rule.state.until)}`
      : `Time's up, opens again at ${when(rule.state.until)}`));
  } else if (rule.left_s != null) {
    const used = Math.max(0, rule.budget_s - rule.left_s);
    row.append(el("div", "line", `${duration(rule.left_s)} left \u00b7 ${rule.label}`));
    row.append(bar(rule.left_s, rule.budget_s));
    row.append(el("div", "line", `${Math.floor(used / 60)} of ${Math.round(rule.budget_s / 60)} min used, resets ${when(rule.resets_ms)}`));
  } else if (!rule.next_window) {
    row.append(el("div", "line", "No limit applies right now."));
  }
  if (rule.next_window && !(rule.state && rule.state.reason === "window")) {
    const [start, end] = rule.next_window;
    row.append(el("div", "line", `Blocked ${when(start)}\u2013${clock(end)}`));
  }
  if (rule.pass_until_ms > Date.now()) {
    row.append(el("div", "line", `Extra time until ${clock(rule.pass_until_ms)}`));
  }
  return row;
}

function render(data) {
  const status = $("status");
  status.textContent = data.offline ? "Busyist not running" : "Connected";
  status.className = "status " + (data.offline ? "bad" : "ok");

  // Help: what to do when something is missing. The extension can't open the
  // app, so it says where to find it.
  const help = $("help");
  help.className = "notice hidden";
  help.replaceChildren();
  const tip = (info, ...parts) => { help.className = "notice" + (info ? " info" : ""); help.append(...parts); };
  if (data.offline) {
    tip(false, el("b", "", "Busyist isn't running. "),
      data.known ? "Limits keep working from what it last said, and time is saved until it's back. "
                 : "Nothing is blocked until it runs. ",
      "Start Busyist from the Start menu, or open it from its tray icon.");
  } else if (!data.limits_on && !data.limits.length) {
    tip(true, el("b", "", "Website limits are off. "),
      "In Busyist, open Settings \u2192 Limits, add a website and turn on \u201cEnforce website limits\u201d.");
  } else if (!data.limits_on) {
    tip(true, el("b", "", "Website limits are switched off. "),
      "Turn on \u201cEnforce website limits\u201d in Busyist \u2192 Settings \u2192 Limits.");
  } else if (!data.limits.length) {
    tip(true, el("b", "", "No limits yet. "),
      "Open Busyist, then Settings \u2192 Limits \u2192 Add a website limit (for example 5 minutes per hour).");
  }

  renderFocus(data);

  const here = data.limits.filter((r) => r.here);
  $("hereCard").classList.toggle("hidden", !here.length);
  $("here").replaceChildren(...here.map((rule) => {
    const box = el("div");
    if (rule.state) box.append(el("div", "big bad", rule.state.reason === "window" ? "Blocked" : "Time's up"));
    else if (rule.left_s != null) {
      box.append(el("div", "big " + (rule.left_s <= 30 ? "bad" : rule.left_s <= 120 ? "warn" : "ok"), duration(rule.left_s) + " left"));
      box.append(bar(rule.left_s, rule.budget_s));
      box.append(el("div", "small muted", rule.label));
    } else {
      box.append(el("div", "small", "No time limit right now."));
    }
    return box;
  }));

  $("limitsCard").classList.toggle("hidden", !data.limits.length);
  $("limits").replaceChildren(...data.limits.map(ruleRow));

  const footer = $("footer");
  footer.replaceChildren();
  footer.append("Change limits in ", el("b", "", "Busyist \u2192 Settings \u2192 Limits"),
    ". Blocking during pomodoros is in the Websites tab. ", el("span", "", `Extension ${data.version}`));
}

async function load() {
  try {
    render(await chrome.runtime.sendMessage({ type: "popup" }));
  } catch (e) {
    $("status").textContent = "Starting\u2026"; // the worker is waking up; try again shortly
  }
}

load();
setInterval(load, 2000);
