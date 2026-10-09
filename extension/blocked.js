// The page a blocked site is redirected to. Two kinds of block land here:
//
//  * focus: a pomodoro work phase is running. Shows the task and a live
//    countdown, and sends the tab back the moment focus ends.
//  * limit (?l=<rule id>): a site's time budget is used up, or it's inside
//    blocked hours. Shows why and when it opens again, and offers a short pass.
//
// It asks the extension's background worker where things stand, so it also
// works (from the last known state) while Busyist is closed.

// The original URL is everything after "u=" (passed raw, so no decoding).
const marker = location.href.search(/[?&]u=/);
const original = marker >= 0 ? location.href.slice(marker + 3) : "";
const limitId = new URLSearchParams(location.search).get("l") || "";
const $ = (id) => document.getElementById(id);

try {
  $("url").textContent = original ? new URL(original).hostname : "";
} catch (e) {
  $("url").textContent = "";
}

function goBack() {
  if (original) location.replace(original);
  else history.length > 1 ? history.back() : location.replace("about:blank");
}

const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const when = (ms) => {
  const day = new Date(ms).toDateString() === new Date().toDateString()
    ? "" : new Date(ms).toLocaleDateString([], { weekday: "short" }) + " ";
  return day + clock(ms);
};
const minutes = (s) => Math.round(s / 60);

let endsAt = 0;
let passMinutes = 2;

// Ticks the focus countdown. A limit page's line ("Opens again at") is left
// alone: clearing it here between asks made it blink.
function renderCount() {
  if (limitId) return;
  const count = $("count");
  if (!endsAt) { count.textContent = ""; return; }
  const mins = Math.ceil(Math.max(0, endsAt - Date.now()) / 60000);
  const html = `Focus until <b>${clock(endsAt)}</b> \u00b7 <b>${mins}</b> min left`;
  if (count.innerHTML !== html) count.innerHTML = html;
}

// Sets text only when it changed, so nothing flickers on a re-render.
function setText(id, text) {
  if ($(id).textContent !== text) $(id).textContent = text;
}

function render(status) {
  if (!status) return; // no answer this round: keep what's shown
  const { focus, limit } = status;
  if (limitId) {
    if (!limit) { goBack(); return; }
    const eyebrow = $("eyebrow").lastChild;
    if (eyebrow.textContent !== "Busyist limit") eyebrow.textContent = "Busyist limit";
    if (limit.reason === "window") {
      setText("title", "Blocked right now.");
      setText("task", `${limit.pattern} is blocked at this time.`);
    } else {
      setText("title", "That's enough for now.");
      setText("task", `You've used ${minutes(limit.used_s)} of ${minutes(limit.budget_s)} min on ${limit.pattern} (${limit.label}).`);
    }
    endsAt = 0;
    const count = `Opens again at <b>${when(limit.until)}</b>`;
    if ($("count").innerHTML !== count) $("count").innerHTML = count;
    passMinutes = limit.pass_minutes;
    setText("pass", `Allow ${passMinutes} more minutes`);
    $("pass").classList.remove("hidden");
    return;
  }
  if (!focus.active) { goBack(); return; }
  setText("task", focus.task || "");
  endsAt = focus.ends_at_ms || 0;
  renderCount();
}

async function check() {
  try {
    render(await chrome.runtime.sendMessage({ type: "status", limit: limitId }));
  } catch (e) {
    // The worker is waking up; ask again on the next round.
  }
}

$("pass").onclick = async () => {
  $("pass").disabled = true;
  try {
    render(await chrome.runtime.sendMessage({ type: "grant", id: limitId, limit: limitId }));
  } catch (e) { /* the next check picks it up */ }
  $("pass").disabled = false;
};

check();
setInterval(check, 4000);   // re-ask Busyist
setInterval(renderCount, 1000); // tick the countdown between asks
