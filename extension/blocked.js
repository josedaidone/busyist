// The "back to work" page a blocked site is redirected to. It shows the task
// and a live countdown, keeps asking Busyist whether focus is still on, and
// sends the tab back to where it came from the moment it isn't.

const FOCUS_URL = "http://127.0.0.1:47616/focus";

// The original URL is everything after "?u=" (passed raw, so no decoding).
const marker = location.href.indexOf("?u=");
const original = marker >= 0 ? location.href.slice(marker + 3) : "";

try {
  document.getElementById("url").textContent = original ? new URL(original).hostname : "";
} catch (e) {
  document.getElementById("url").textContent = "";
}

function goBack() {
  if (original) location.replace(original);
  else history.length > 1 ? history.back() : location.replace("about:blank");
}

let endsAt = 0;

function renderCount() {
  const count = document.getElementById("count");
  if (!endsAt) { count.textContent = ""; return; }
  const left = Math.max(0, endsAt - Date.now());
  const mins = Math.ceil(left / 60000);
  const until = new Date(endsAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  count.innerHTML = `Focus until <b>${until}</b> &middot; <b>${mins}</b> min left`;
}

async function check() {
  let focus;
  try {
    const res = await fetch(FOCUS_URL, { cache: "no-store" });
    focus = res.ok ? await res.json() : { active: false };
  } catch (e) {
    focus = { active: false }; // Busyist closed or unreachable: let the tab back
  }
  if (!focus.active) { goBack(); return; }
  document.getElementById("task").textContent = focus.task || "";
  endsAt = focus.ends_at_ms || 0;
  renderCount();
}

check();
setInterval(check, 4000);   // re-ask Busyist
setInterval(renderCount, 1000); // tick the countdown between asks
