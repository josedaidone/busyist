// Busyist Focus - MV3 service worker.
//
// Two jobs, both driven by what the local Busyist app says (see /focus):
//
// 1. Focus blocking (issue #2): while a pomodoro work phase runs, redirect
//    the blocked sites to blocked.html. In "block" mode the listed sites are
//    blocked; in "allow" mode every site is, except the listed ones. No
//    session, a break, a pause, or Busyist being closed all report
//    active:false, which removes those rules.
//
// 2. Site limits: per-website time budgets ("5 min per hour") and blocked
//    hours, all day, pomodoro or not. This worker measures the time you spend
//    on each limited site (active tab of the focused window, not idle, not
//    locked), reports it to Busyist, and redirects a site once it's used up or
//    inside blocked hours. Busyist keeps the totals; if it isn't running, the
//    last answer is cached and enforced, and the time measured meanwhile is
//    kept and sent when Busyist is back.

const BASE = "http://127.0.0.1:47616";
const VERSION = chrome.runtime.getManifest().version;
// Busyist compares this with the extension it ships, to say when to reload.
const FOCUS_URL = BASE + "/focus?v=" + VERSION;
const USAGE_URL = BASE + "/usage";
const GRANT_URL = BASE + "/grant";
const BLOCKED = chrome.runtime.getURL("blocked.html");
// Higher wins. Limits beat the allow rules, so a limited site stays blocked in
// allow mode; focus blocks in "block" mode beat limits, so the focus page shows.
const CATCH_ALL_PRIORITY = 1;
const ALLOW_PRIORITY = 2;
const LIMIT_PRIORITY = 3;
const FOCUS_PRIORITY = 4;
const LIMIT_RULE_BASE = 1000;
const ALARM = "busyist-sync";
const IDLE_SECONDS = 60; // no input for this long: the time isn't counted
const TICK_MS = 5000; // how often time is measured while a limited site is in use
const MAX_GAP_S = 65; // a longer gap than this (sleep, worker killed) isn't counted

// ----------------------------------------------------------------- state

let lastGood = null; // the last answer from Busyist, kept across restarts
let pending = {}; // {ruleId: {epochMinute: seconds}} measured, not yet sent
let passes = {}; // {ruleId: untilMs} granted from the block page while Busyist was out
let counting = { ids: [], since: 0 }; // the rules being timed right now
let timer = null;
let cache = { at: 0, data: null };

const ready = chrome.storage.local.get(["lastGood", "pending", "passes"]).then((s) => {
  lastGood = s.lastGood || null;
  pending = s.pending || {};
  passes = s.passes || {};
});

const saveState = () => chrome.storage.local.set({ lastGood, pending, passes });

// ------------------------------------------------------------- patterns
//
// A site entry (tidied by Busyist's clean_site_pattern) is a host, optionally
// followed by a path, where * matches anything:
//   youtube.com           youtube.com and its subdomains, any page
//   *.google.com          any subdomain of google.com (not google.com itself)
//   google.*              google.com, google.co.uk, ... (www. allowed)
//   youtube.com/shorts/*  only those pages (a path without * also covers
//                         the pages below it: reddit.com/r/foo)
// Each one becomes an RE2 regex for declarativeNetRequest (also valid as a JS
// RegExp, for the tab sweep); group 1 is always the whole URL.

const escapeRe = (s) => s.replace(/[.+?^${}()|[\]\\]/g, "\\$&");
const glob = (s, star) => s.split("*").map(escapeRe).join(star);

function patternRegex(pattern) {
  const slash = pattern.indexOf("/");
  const host = slash < 0 ? pattern : pattern.slice(0, slash);
  const path = slash < 0 ? "" : pattern.slice(slash);
  const hostRe = host.includes("*")
    ? "(?:www\\.)?" + glob(host, "[^/?#]*")
    : "(?:[^/?#]*\\.)?" + escapeRe(host);
  let pathRe = "(?:[/?#].*)?";
  if (path) pathRe = glob(path, ".*") + (path.endsWith("*") ? "" : "(?:[/?#].*)?");
  return "^(https?://" + hostRe + "(?::\\d+)?" + pathRe + ")$";
}

function matches(pattern, url) {
  try { return new RegExp(patternRegex(pattern), "i").test(url); } catch (e) { return false; }
}

// Busyist versions before allowlists only send `sites` (domains to block).
function rulesOf(focus) {
  if (Array.isArray(focus.patterns)) {
    return { mode: focus.mode === "allow" ? "allow" : "block", patterns: focus.patterns };
  }
  return { mode: "block", patterns: focus.sites || [] };
}

function focusBlocks(url, focus) {
  if (!focus.active || !/^https?:\/\//i.test(url)) return false;
  const { mode, patterns } = rulesOf(focus);
  const hit = patterns.some((p) => matches(p, url));
  return mode === "allow" ? !hit : hit;
}

// ---------------------------------------------------------------- limits

const pendingSeconds = (id) =>
  Object.values(pending[id] || {}).reduce((sum, s) => sum + s, 0);

// Is this limit rule blocking right now, and until when? Works from Busyist's
// last answer plus the time measured since, so it keeps enforcing without it.
function limitState(rule, now) {
  if (Math.max(rule.pass_until_ms || 0, passes[rule.id] || 0) > now) return null;
  const span = (rule.windows_ms || []).find(([start, end]) => start <= now && now < end);
  if (span) return { reason: "window", until: span[1] };
  if (rule.remaining_s != null && now < rule.limit_end_ms
      && rule.remaining_s - pendingSeconds(rule.id) <= 0) {
    return { reason: "budget", until: rule.limit_end_ms };
  }
  return null;
}

const patternsOf = (rule) => rule.patterns || [rule.pattern];

const limitRules = (focus) => (focus && focus.limits_on && focus.limits) || [];

function limitBlocks(url, focus, now) {
  if (!/^https?:\/\//i.test(url)) return null;
  for (const rule of limitRules(focus)) {
    if (patternsOf(rule).some((p) => matches(p, url)) && limitState(rule, now)) return rule;
  }
  return null;
}

function addPending(ids, seconds, now) {
  const minute = Math.floor(now / 60000);
  // Busyist ignores anything older than 3 days, so don't keep it either.
  for (const per of Object.values(pending)) {
    for (const m of Object.keys(per)) if (Number(m) < minute - 3 * 1440) delete per[m];
  }
  for (const id of ids) {
    const per = (pending[id] = pending[id] || {});
    per[minute] = Math.min(60, (per[minute] || 0) + seconds);
  }
}

// The page the user is looking at: the active tab of the focused window.
async function activeUrl() {
  const win = await chrome.windows.getLastFocused().catch(() => null);
  if (!win || !win.focused) return "";
  const [tab] = await chrome.tabs.query({ active: true, windowId: win.id });
  return (tab && tab.url) || "";
}

const rulesFor = (focus, url) =>
  limitRules(focus).filter((r) => patternsOf(r).some((p) => matches(p, url)));

// Seconds left on a rule's time budget, or null if it has none that applies
// (or the answer is too old to say). Counts the time measured since Busyist
// was last asked.
function secondsLeft(rule, now) {
  if (rule.remaining_s == null || now >= rule.limit_end_ms) return null;
  return Math.max(0, rule.remaining_s - pendingSeconds(rule.id));
}

// Which limited rules is the user using right now? Their current page, while
// they're at the PC.
async function currentIds(focus, now, url) {
  if (!url || !limitRules(focus).length) return [];
  const idle = await chrome.idle.queryState(IDLE_SECONDS);
  if (idle !== "active") return [];
  return rulesFor(focus, url).filter((r) => !limitState(r, now)).map((r) => r.id);
}

// --------------------------------------------------------- talking to Busyist

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  return res.ok ? res.json() : null;
}

// Send what's been measured and get the current answer back. Without Busyist
// (not running, or an old one with no /usage) fall back to what we last knew:
// focus blocking lets go, limits keep being enforced.
async function fetchState(force) {
  const hasPending = Object.keys(pending).length > 0;
  if (!force && !hasPending && cache.data && Date.now() - cache.at < TICK_MS) return cache.data;
  let data = null;
  const sent = JSON.parse(JSON.stringify(pending));
  try {
    data = await post(USAGE_URL, { usage: sent, version: VERSION });
    if (data) {
      // What we sent is now in Busyist's totals; anything measured while the
      // request was out stays pending.
      for (const [id, per] of Object.entries(sent)) {
        for (const [minute, seconds] of Object.entries(per)) {
          const left = ((pending[id] || {})[minute] || 0) - seconds;
          if (left > 0.01) pending[id][minute] = left;
          else if (pending[id]) delete pending[id][minute];
        }
        if (pending[id] && !Object.keys(pending[id]).length) delete pending[id];
      }
    } else {
      const res = await fetch(FOCUS_URL, { cache: "no-store" }); // an older Busyist
      data = res.ok ? await res.json() : null;
    }
  } catch (e) {
    data = null;
  }
  if (data) {
    lastGood = data;
    // Remember a pass that Busyist now knows about, and drop the local copy.
    for (const rule of limitRules(data)) if (rule.pass_until_ms) delete passes[rule.id];
  } else {
    // Busyist isn't running or can't be reached. Focus blocking fails open;
    // limits go on from the last answer.
    data = Object.assign({}, lastGood || {}, { active: false, patterns: [], sites: [], offline: true });
  }
  cache = { at: Date.now(), data };
  saveState();
  return data;
}

// ----------------------------------------------------------------- rules

function redirectRule(id, priority, regexFilter, limitId) {
  const query = limitId ? "?l=" + limitId + "&u=" : "?u=";
  return {
    id,
    priority,
    // \1 is the whole URL, handed to blocked.html so it can go back later.
    action: { type: "redirect", redirect: { regexSubstitution: BLOCKED + query + "\\1" } },
    condition: { regexFilter, isUrlFilterCaseSensitive: false, resourceTypes: ["main_frame"] },
  };
}

async function setRules(focus, now) {
  const existing = await chrome.declarativeNetRequest.getDynamicRules();
  const removeRuleIds = existing.map((r) => r.id);
  const addRules = [];
  const { mode, patterns } = rulesOf(focus);
  if (focus.active && mode === "allow") {
    addRules.push(redirectRule(1, CATCH_ALL_PRIORITY, "^(https?://.*)$"));
    patterns.forEach((p, i) => addRules.push({
      id: i + 2,
      priority: ALLOW_PRIORITY,
      action: { type: "allow" },
      condition: { regexFilter: patternRegex(p), isUrlFilterCaseSensitive: false, resourceTypes: ["main_frame"] },
    }));
  } else if (focus.active) {
    patterns.forEach((p, i) => addRules.push(redirectRule(i + 1, FOCUS_PRIORITY, patternRegex(p))));
  }
  let next = LIMIT_RULE_BASE;
  for (const rule of limitRules(focus)) {
    if (!limitState(rule, now)) continue;
    for (const p of patternsOf(rule)) {
      addRules.push(redirectRule(next++, LIMIT_PRIORITY, patternRegex(p), rule.id));
    }
  }
  try {
    await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds, addRules });
  } catch (e) {
    // One rejected regex fails the whole batch; add the rules one by one.
    console.warn("Busyist: rules rejected, adding them one at a time", e);
    await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds, addRules: [] });
    for (const rule of addRules) {
      try {
        await chrome.declarativeNetRequest.updateDynamicRules({ addRules: [rule] });
      } catch (err) {
        console.warn("Busyist: skipped rule", rule.condition.regexFilter, err);
      }
    }
  }
}

async function sweepTabs(focus, now) {
  // Tabs already sitting on a blocked site when it becomes blocked: send them over too.
  const tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  for (const tab of tabs) {
    if (!tab.id || !tab.url) continue;
    if (focusBlocks(tab.url, focus)) {
      chrome.tabs.update(tab.id, { url: BLOCKED + "?u=" + tab.url });
    } else {
      const rule = limitBlocks(tab.url, focus, now);
      if (rule) chrome.tabs.update(tab.id, { url: BLOCKED + "?l=" + rule.id + "&u=" + tab.url });
    }
  }
}

// 45s, 12m, 3h: short enough for the badge.
function shortTime(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  if (s < 60) return s + "s";
  const m = Math.ceil(s / 60);
  return m < 100 ? m + "m" : Math.floor(m / 60) + "h";
}

// The badge shows the time left on the current page's limit, or else the
// minutes left in the pomodoro work phase; the tooltip says which.
function updateBadge(focus, now, url) {
  const here = (url ? rulesFor(focus, url) : [])
    .map((rule) => ({ rule, left: secondsLeft(rule, now) }))
    .filter((x) => x.left != null && !limitState(x.rule, now))
    .sort((a, b) => a.left - b.left)[0];
  if (here) {
    chrome.action.setBadgeText({ text: shortTime(here.left) });
    chrome.action.setBadgeBackgroundColor({ color: here.left <= 30 ? "#db4035" : here.left <= 120 ? "#e08a00" : "#2e9e6b" });
    chrome.action.setTitle({ title: `Busyist: ${shortTime(here.left)} left on ${patternsOf(here.rule).join(", ")} (${here.rule.limit_label})` });
  } else if (focus.active && focus.ends_at_ms) {
    const mins = Math.max(0, Math.round((focus.ends_at_ms - now) / 60000));
    chrome.action.setBadgeText({ text: String(mins) });
    chrome.action.setBadgeBackgroundColor({ color: "#db4035" });
    chrome.action.setTitle({ title: `Busyist: focus, ${mins} min left` });
  } else {
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "Busyist Focus" });
  }
}

// ------------------------------------------------------------------ tick

// Everything happens in one place, one at a time: credit the time since the
// last tick, exchange it with Busyist, apply the rules, work out what's being
// timed now.
let chain = Promise.resolve();
const sync = (force) => (chain = chain.then(() => tick(force)).catch((e) => console.warn("Busyist", e)));

async function tick(force) {
  await ready;
  let now = Date.now();
  if (counting.ids.length) {
    addPending(counting.ids, Math.min((now - counting.since) / 1000, MAX_GAP_S), now);
  }
  const focus = await fetchState(force);
  now = Date.now();
  await setRules(focus, now);
  await sweepTabs(focus, now);
  const url = await activeUrl();
  counting = { ids: await currentIds(focus, now, url), since: Date.now() };
  updateBadge(focus, Date.now(), url);
  // While something is being timed, keep ticking (and keep the worker awake).
  if (counting.ids.length && !timer) timer = setInterval(() => sync(false), TICK_MS);
  if (!counting.ids.length && timer) { clearInterval(timer); timer = null; }
}

function ensureAlarm() {
  // 30 s is the MV3 minimum; it also wakes the worker back up after it sleeps.
  chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
}

chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); sync(true); });
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); sync(true); });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) sync(true); });
chrome.tabs.onUpdated.addListener((_id, info) => { if (info.status === "loading" || info.url) sync(false); });
chrome.tabs.onActivated.addListener(() => sync(false));
chrome.tabs.onRemoved.addListener(() => sync(false));
chrome.windows.onFocusChanged.addListener(() => sync(false));
chrome.idle.setDetectionInterval(IDLE_SECONDS);
chrome.idle.onStateChanged.addListener(() => sync(false));

// ------------------------------------------------------- the block page

// blocked.js asks where things stand (so it works while Busyist is closed too)
// and can grant a short pass ("allow 2 more minutes").
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    await ready;
    const now = Date.now();
    if (msg.type === "grant") {
      const minutes = (lastGood && lastGood.pass_minutes) || 2;
      passes[msg.id] = now + minutes * 60000;
      saveState();
      try { await post(GRANT_URL, { id: msg.id, version: VERSION }); } catch (e) { /* the local pass still holds */ }
      await sync(true);
    }
    if (msg.type === "popup") { reply(await popupData(now)); return; }
    const focus = (await fetchState(msg.type === "grant")) || {};
    const rule = limitRules(focus).find((r) => r.id === msg.limit);
    const state = rule && limitState(rule, now);
    reply({
      focus: { active: !!focus.active, task: focus.task || "", ends_at_ms: focus.ends_at_ms || 0 },
      limit: rule && state ? {
        reason: state.reason,
        until: state.until,
        label: rule.limit_label,
        used_s: rule.used_s,
        budget_s: rule.budget_s,
        pattern: patternsOf(rule).join(", "),
        pass_minutes: focus.pass_minutes || 2,
      } : null,
    });
  })();
  return true; // reply asynchronously
});

// What the toolbar popup shows (popup.js): the pomodoro, the limits and where
// each stands, and whether Busyist is reachable.
async function popupData(now) {
  const focus = (await fetchState(false)) || {};
  const url = await activeUrl();
  const here = new Set((url ? rulesFor(focus, url) : []).map((r) => r.id));
  const upcoming = (rule) => (rule.windows_ms || []).find(([start]) => start > now);
  return {
    version: VERSION,
    offline: !!focus.offline || !lastGood,
    known: !!lastGood,
    focus: {
      active: !!focus.active, task: focus.task || "", ends_at_ms: focus.ends_at_ms || 0,
      mode: focus.mode || "block", sites: (focus.patterns || []).length,
    },
    limits_on: !!focus.limits_on,
    pass_minutes: focus.pass_minutes || 2,
    limits: limitRules(focus).map((rule) => {
      const state = limitState(rule, now);
      const next = upcoming(rule);
      return {
        id: rule.id,
        patterns: patternsOf(rule),
        here: here.has(rule.id),
        state,                                  // {reason, until} while blocked
        left_s: (() => {                        // null: no time limit applies now
          const left = secondsLeft(rule, now);
          const live = counting.ids.includes(rule.id) ? Math.min((now - counting.since) / 1000, MAX_GAP_S) : 0;
          return left == null ? null : Math.max(0, left - live);
        })(),
        budget_s: rule.budget_s,
        label: rule.limit_label,
        resets_ms: rule.limit_end_ms,
        next_window: next || null,              // [start, end] of the next blocked hours
        pass_until_ms: Math.max(rule.pass_until_ms || 0, passes[rule.id] || 0),
      };
    }),
  };
}

// Also run when the worker first loads (e.g. right after "Load unpacked").
ensureAlarm();
sync(true);
