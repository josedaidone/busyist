"use strict";

const $ = (id) => document.getElementById(id);
const RING = 2 * Math.PI * 33;
let api = null;
let state = null;

function clock(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

function render() {
  if (!state) return;
  const t = state.timer, s = state.session;
  const phase = !t ? "idle" : t.paused ? "paused" : t.phase === "work" ? "work" : "rest";
  document.body.dataset.phase = phase;
  const left = t ? Math.max(0, t.paused ? t.left_ms : t.left_ms - (Date.now() - state.now)) : 0;
  $("mTime").textContent = t ? clock(left) : "--:--";
  $("mProgress").style.strokeDasharray = RING;
  $("mProgress").style.strokeDashoffset = t ? RING * (1 - left / t.total_ms) : RING;
  $("mPhase").textContent = { idle: "Idle", paused: "Paused", work: "Focus", rest: "Break" }[phase];
  $("mRound").textContent = t ? `· ${t.round}/${t.rounds}` : "";
  $("mTask").textContent = s ? s.task.content : t ? "Running on the bar" : "No session";
  $("mProject").textContent = s ? s.task.project : "";
  $("mPauseIcon").setAttribute("href", t && t.paused ? "#i-play" : "#i-pause");
  $("mPause").title = t && t.paused ? "Resume" : "Pause";
  $("mPause").style.display = $("mStop").style.display = t ? "" : "none";
}

async function poll() {
  try {
    state = await api.get_state();
    render();
  } catch (err) {
    /* the window is being torn down */
  }
}

// Buttons must not start a window drag.
for (const b of document.querySelectorAll(".m-actions button")) {
  b.addEventListener("mousedown", (e) => e.stopPropagation());
}
$("mPause").onclick = async () => {
  await api.control(state.timer && state.timer.paused ? "resume" : "pause");
  poll();
};
$("mStop").onclick = async () => { await api.control("stop"); poll(); };
$("mOpen").onclick = () => api.show_main();
$("mHide").onclick = () => api.hide_mini();
$("mini").addEventListener("dblclick", () => api.show_main());

window.Mini = { refresh: poll };

function boot() {
  api = window.pywebview.api;
  poll();
  setInterval(poll, 1000);
  setInterval(render, 250);
}

if (window.pywebview && window.pywebview.api) boot();
else window.addEventListener("pywebviewready", boot, { once: true });
