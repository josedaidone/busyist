// buZyist Focus - MV3 service worker.
//
// Two jobs, both driven by what the local buZyist app says (see /focus):
//
// 1. Focus blocking (issue #2): while a pomodoro work phase runs, redirect
//    the blocked sites to blocked.html. In "block" mode the listed sites are
//    blocked; in "allow" mode every site is, except the listed ones. No
//    session, a break, a pause, or buZyist being closed all report
//    active:false, which removes those rules.
//
// 2. Site limits: per-website time budgets ("5 min per hour") and blocked
//    hours, all day, pomodoro or not. This worker measures the time you spend
//    on each limited site (active tab of the focused window, not idle, not
//    locked), reports it to buZyist, and redirects a site once it's used up or
//    inside blocked hours. buZyist keeps the totals; if it isn't running, the
//    last answer is cached and enforced, and the time measured meanwhile is
//    kept and sent when buZyist is back.
//
// 3. Sharing limits, and the running task, across PCs: see "sharing across
//    PCs" below.

const BASE = "http://127.0.0.1:47616";
const VERSION = chrome.runtime.getManifest().version;
// buZyist compares this with the extension it ships, to say when to reload.
const FOCUS_URL = BASE + "/focus?v=" + VERSION;
const USAGE_URL = BASE + "/usage";
const GRANT_URL = BASE + "/grant";
const SYNC_URL = BASE + "/sync";
// Web pages can make plain GETs to 127.0.0.1 too (an <img>, say), but can't add
// a header of their own without a preflight, which buZyist refuses. Only
// requests with this header may register this profile or get `sync`.
const HEADERS = { "X-Buzyist": "1" };
const BLOCKED = chrome.runtime.getURL("blocked.html");
// Higher wins; at equal priority Chrome prefers "allow" over "redirect".
// An activity's exceptions (`except`) in block mode are allow rules at the focus
// blocks' priority, so they get through them (mail.google.com inside
// *.google.com). Limits beat everything, so a site out of budget stays blocked
// even when it's allowed or excepted; a site both focus-blocked and over its
// limit shows the limit page, and its pass falls through to the focus block.
const CATCH_ALL_PRIORITY = 1;
const ALLOW_PRIORITY = 2;
const FOCUS_PRIORITY = 4;
const EXCEPT_PRIORITY = 4;
const LIMIT_PRIORITY = 5;
const LIMIT_RULE_BASE = 1000;
const ALARM = "busyist-sync";
const IDLE_SECONDS = 60; // no input for this long: the time isn't counted
const TICK_MS = 5000; // how often time is measured while a limited site is in use
const MAX_GAP_S = 65; // a longer gap than this (sleep, worker killed) isn't counted

// ----------------------------------------------------------------- state

let lastGood = null; // the last answer from buZyist, kept across restarts
let pending = {}; // {ruleId: {epochMinute: seconds}} measured, not yet sent
let passes = {}; // {ruleId: untilMs} granted from the block page while buZyist was out
let blocks = []; // [{id, at, kind, host}] sites the block page showed, not yet sent (the Stats tab)
let counting = { ids: [], since: 0 }; // the rules being timed right now
let timer = null;
let cache = { at: 0, data: null };
// This browser profile: a random id kept for good, and its Google account (""
// when not signed in). buZyist uses them to pick the one profile per PC that
// shares limits through Chrome sync, and to name it in Settings.
const me = { id: "", account: "" };

const ready = chrome.storage.local.get(["lastGood", "pending", "passes", "blocks", "instanceId"]).then(async (s) => {
  lastGood = s.lastGood || null;
  pending = s.pending || {};
  passes = s.passes || {};
  blocks = Array.isArray(s.blocks) ? s.blocks : [];
  me.id = s.instanceId || "";
  if (!/^[a-z0-9]{6,32}$/.test(me.id)) {
    me.id = crypto.randomUUID().replace(/-/g, "").slice(0, 16);
    await chrome.storage.local.set({ instanceId: me.id });
  }
  me.account = await profileEmail();
});

// Needs the identity.email permission; "" if the profile isn't signed in.
function profileEmail() {
  return new Promise((resolve) => {
    try {
      chrome.identity.getProfileUserInfo({ accountStatus: "ANY" }, (info) => {
        void chrome.runtime.lastError;
        resolve((info && info.email) || "");
      });
    } catch (e) {
      resolve("");
    }
  });
}

const who = () => ({ version: VERSION, instance: me.id, account: me.account });
const focusUrl = () => FOCUS_URL + "&i=" + me.id + "&a=" + encodeURIComponent(me.account);

// Written only when something changed: most ticks change nothing.
let savedSig = null;
function saveState() {
  const sig = canon({ lastGood, pending, passes, blocks });
  if (sig === savedSig) return Promise.resolve();
  savedSig = sig;
  return chrome.storage.local.set({ lastGood, pending, passes, blocks }).catch((e) => {
    savedSig = null; // try again next time
    throw e;
  });
}

// ------------------------------------------------------------- patterns
//
// A site entry (tidied by buZyist's clean_site_pattern) is a host, optionally
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

// Every tab is tested against every pattern on each pass, so each pattern is
// compiled once. Without the g flag a RegExp keeps no state between tests.
const regexCache = new Map(); // pattern -> RegExp, or null when it doesn't compile
function compiled(pattern) {
  let re = regexCache.get(pattern);
  if (re === undefined) {
    try { re = new RegExp(patternRegex(pattern), "i"); } catch (e) { re = null; }
    if (regexCache.size > 2000) regexCache.clear(); // patterns come and go with Settings edits
    regexCache.set(pattern, re);
  }
  return re;
}

function matches(pattern, url) {
  const re = compiled(pattern);
  return !!re && re.test(url);
}

// buZyist versions before allowlists only send `sites` (domains to block), and
// those before activities (2.5.0) no `except`: the patterns that go the other
// way inside the mode's list (let through in "block", blocked in "allow").
function rulesOf(focus) {
  const carve = Array.isArray(focus.except) ? focus.except : [];
  if (Array.isArray(focus.patterns)) {
    return { mode: focus.mode === "allow" ? "allow" : "block", patterns: focus.patterns, except: carve };
  }
  return { mode: "block", patterns: focus.sites || [], except: carve };
}

function focusBlocks(url, focus) {
  if (!focus.active || !/^https?:\/\//i.test(url)) return false;
  const { mode, patterns, except } = rulesOf(focus);
  const hit = patterns.some((p) => matches(p, url));
  const carved = except.some((p) => matches(p, url));
  return mode === "allow" ? !hit || carved : hit && !carved;
}

// ---------------------------------------------------------------- limits

const pendingSeconds = (id) =>
  Object.values(pending[id] || {}).reduce((sum, s) => sum + s, 0);

// Is this limit rule blocking right now, and until when? Works from buZyist's
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

// One block shown (blocked.js says so once per page). Kept until buZyist has
// it; the id lets buZyist count a report sent twice only once.
const MAX_BLOCKS = 500;
function addBlock(kind, host, now) {
  if ((kind !== "focus" && kind !== "limit") || typeof host !== "string" || !host) return;
  blocks.push({ id: crypto.randomUUID().replace(/-/g, "").slice(0, 16), at: now, kind, host: host.toLowerCase() });
  blocks = blocks.filter((b) => b.at > now - 3 * 86400000).slice(-MAX_BLOCKS); // buZyist ignores older ones
}

// "YYYY-MM-DD" in local time, as buZyist names days.
function localDay(ms) {
  const d = new Date(ms);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

// Today's blocks: buZyist's last count plus the ones not sent yet.
function blocksToday(now) {
  const day = localDay(now);
  const known = lastGood && lastGood.blocks_today && lastGood.blocks_today.day === day ? lastGood.blocks_today : {};
  const out = { focus: known.focus || 0, limit: known.limit || 0 };
  for (const b of blocks) if (localDay(b.at) === day) out[b.kind] += 1;
  return out;
}

function addPending(ids, seconds, now) {
  const minute = Math.floor(now / 60000);
  // buZyist ignores anything older than 3 days, so don't keep it either.
  for (const per of Object.values(pending)) {
    for (const m of Object.keys(per)) if (Number(m) < minute - 3 * 1440) delete per[m];
  }
  for (const id of ids) {
    const per = (pending[id] = pending[id] || {});
    per[minute] = Math.min(60, (per[minute] || 0) + seconds);
  }
}

// The page the user is looking at: the active tab of the last browser window
// they used. With `needFocus` only while that window has the keyboard focus
// (that's what counts as using it); without it also while the toolbar popup
// or another app is in front, which is when the popup and badge still want to
// know the page.
async function activeUrl(needFocus) {
  const win = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => null);
  if (!win || (needFocus && !win.focused)) return "";
  const [tab] = await chrome.tabs.query({ active: true, windowId: win.id });
  return (tab && tab.url) || "";
}

const rulesFor = (focus, url) =>
  limitRules(focus).filter((r) => patternsOf(r).some((p) => matches(p, url)));

// Seconds left on a rule's time budget, or null if it has none that applies
// (or the answer is too old to say). Counts the time measured since buZyist
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

// --------------------------------------------------------- talking to buZyist

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, HEADERS),
    body: JSON.stringify(body),
    cache: "no-store",
  });
  return res.ok ? res.json() : null;
}

// Send what's been measured and get the current answer back. Without buZyist
// (not running, or an old one with no /usage) fall back to what we last knew:
// focus blocking lets go, limits keep being enforced.
async function fetchState(force) {
  const hasPending = Object.keys(pending).length > 0 || blocks.length > 0;
  if (!force && !hasPending && cache.data && Date.now() - cache.at < TICK_MS) return cache.data;
  let data = null;
  const sent = JSON.parse(JSON.stringify(pending));
  const sentBlocks = new Set(blocks.map((b) => b.id));
  try {
    data = await post(USAGE_URL, Object.assign({ usage: sent, blocks: blocks.slice() }, who()));
    if (data) {
      blocks = blocks.filter((b) => !sentBlocks.has(b.id));
      // What we sent is now in buZyist's totals; anything measured while the
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
      const res = await fetch(focusUrl(), { cache: "no-store", headers: HEADERS }); // an older buZyist
      data = res.ok ? await res.json() : null;
    }
  } catch (e) {
    data = null;
  }
  if (data && (force || sessionChanged(data))) data = await shareSync(data);
  if (data) {
    lastGood = data;
    // Remember a pass that buZyist now knows about, and drop the local copy.
    for (const rule of limitRules(data)) if (rule.pass_until_ms) delete passes[rule.id];
  } else {
    // buZyist isn't running or can't be reached. Focus blocking fails open;
    // limits go on from the last answer.
    data = Object.assign({}, lastGood || {}, { active: false, patterns: [], sites: [], offline: true });
  }
  cache = { at: Date.now(), data };
  saveState();
  return data;
}

// ---------------------------------------------------- sharing across PCs
//
// With "Share limits and time with my other PCs" on, buZyist picks one
// browser profile per PC (the first to connect, or the one chosen in
// Settings) and only that one gets `sync` in its answers. It keeps two things
// in chrome.storage.sync, which Chrome copies to every browser signed in to
// the same Google account:
//   limits.0, limits.1 ..  the rules, as merged by the last PC to write:
//                          {rev, n, by, on, on_at, deleted, rules} in .0,
//                          more {rev, rules} chunks when they pass 8 KB
//   u.<pc>.<rule id>       one PC's time on one rule: {n: PC name, t: when
//                          written, b, m, d} (see SiteUsage.export)
//   s.<pc>                 the session a PC is running, with "Share the
//                          running task" on: {n, t, stamp, snap, task,
//                          started_at, work_ms, rounds}, removed when it
//                          ends (see App.shared_session)
// buZyist does the merging (App._merge_limits: rule by rule, newest edit
// wins, removals leave tombstones), so this side only carries the shared
// copy to buZyist when its `rev` is new, and writes buZyist's copy back
// when it differs. Time never conflicts: each PC writes only its own keys
// and buZyist adds up the others'. All this happens on the forced exchanges
// (every 30 s, and when Chrome sync brings a change); time is written at
// most once a minute. A session is written as soon as it changes.

const CHUNK_BYTES = 7000;
const USAGE_WRITE_MS = 60000;
const STALE_MS = 40 * 86400000; // a PC not heard from in this long is forgotten
const TORN_MS = 120000; // chunks from different writes for this long: write ours over them
const SESSION_STALE_MS = 86400000; // a session not rewritten in a day: its PC went away
let lastUsageWrite = 0;
let tornSince = 0;

// The shared rules, null if there are none, undefined while a change is
// still arriving (chunks from different writes).
function readLimits(all) {
  const head = all["limits.0"];
  if (!head || typeof head.rev !== "string") return null;
  const rules = [];
  for (let i = 0; i < (head.n || 1); i++) {
    const chunk = all["limits." + i];
    if (!chunk || chunk.rev !== head.rev || !Array.isArray(chunk.rules)) return undefined;
    rules.push(...chunk.rules);
  }
  return { rev: head.rev, on: !!head.on, on_at: head.on_at || 0, deleted: head.deleted || {}, rules };
}

// JSON with sorted keys, to tell whether two copies hold the same thing.
function canon(value) {
  if (Array.isArray(value)) return "[" + value.map(canon).join(",") + "]";
  if (value && typeof value === "object") {
    return "{" + Object.keys(value).sort().map((k) => JSON.stringify(k) + ":" + canon(value[k])).join(",") + "}";
  }
  return JSON.stringify(value === undefined ? null : value);
}

const docOf = (d) => ({ rules: d.rules, deleted: d.deleted, on: !!d.on, on_at: d.on_at || 0 });

// Every other PC's time: {pc: {name, t, rules: {ruleId: {b, m, d}}}}.
function readUsage(all, device, now) {
  const out = {};
  for (const [key, v] of Object.entries(all)) {
    const m = /^u\.([a-z0-9]+)\.([a-f0-9]+)$/.exec(key);
    if (!m || m[1] === device || !v || now - (v.t || 0) > STALE_MS) continue;
    const pc = (out[m[1]] = out[m[1]] || { name: "", t: 0, rules: {} });
    pc.name = v.n || pc.name;
    pc.t = Math.max(pc.t, v.t || 0);
    pc.rules[m[2]] = { b: v.b, m: v.m || {}, d: v.d || {} };
  }
  return out;
}

async function writeLimits(s, all) {
  const doc = s.doc;
  const rev = s.device + "." + Date.now().toString(36);
  const head = { rev, n: 1, by: s.device, on: !!doc.on, on_at: doc.on_at || 0, deleted: doc.deleted || {}, rules: [] };
  const chunks = [head];
  let cur = head;
  for (const rule of doc.rules || []) {
    if (cur.rules.length && JSON.stringify(cur).length + JSON.stringify(rule).length > CHUNK_BYTES) {
      cur = { rev, rules: [] };
      chunks.push(cur);
    }
    cur.rules.push(rule);
  }
  head.n = chunks.length;
  const items = {};
  chunks.forEach((c, i) => { items["limits." + i] = c; });
  await chrome.storage.sync.set(items);
  const stale = Object.keys(all).filter((k) => /^limits\.\d+$/.test(k) && Number(k.slice(7)) >= chunks.length);
  if (stale.length) await chrome.storage.sync.remove(stale);
}

// This PC's time, only what changed, at most once a minute. Also clears
// this PC's keys for rules that are gone and other PCs' long-silent ones.
async function writeUsage(s, all, now) {
  if (now - lastUsageWrite < USAGE_WRITE_MS) return;
  const prefix = "u." + s.device + ".";
  const usage = s.usage || {};
  const items = {};
  for (const [id, u] of Object.entries(usage)) {
    const old = all[prefix + id];
    if (old && old.n === s.name && canon([old.b, old.m, old.d]) === canon([u.b, u.m, u.d])) continue;
    items[prefix + id] = { n: s.name, t: now, b: u.b, m: u.m, d: u.d };
  }
  const drop = Object.entries(all).filter(([k, v]) => k.startsWith("u.") && (k.startsWith(prefix)
    ? !(k.slice(prefix.length) in usage) : !v || now - (v.t || 0) > STALE_MS)).map(([k]) => k);
  if (Object.keys(items).length) await chrome.storage.sync.set(items);
  if (drop.length) await chrome.storage.sync.remove(drop);
  if (Object.keys(items).length || drop.length) lastUsageWrite = now;
}

// The sessions other PCs run: {pc: {n, t, stamp, snap, task, ...}}.
function readSessions(all, device, now) {
  const out = {};
  for (const [key, v] of Object.entries(all)) {
    const m = /^s\.([a-z0-9]+)$/.exec(key);
    if (m && m[1] !== device && v && now - (v.t || 0) < SESSION_STALE_MS) out[m[1]] = v;
  }
  return out;
}

// This PC's session, when it changed; gone when it ended or sharing is off.
// Also clears other PCs' sessions left behind (a PC switched off mid-session).
async function writeSession(s, all, now) {
  const key = "s." + s.device;
  const mine = s.share_session ? s.session || null : null;
  const old = all[key];
  if (mine && !(old && old.n === s.name && canon(Object.assign({}, old, { n: 0, t: 0 }))
      === canon(Object.assign({}, mine, { n: 0, t: 0 })))) {
    await chrome.storage.sync.set({ [key]: Object.assign({}, mine, { n: s.name, t: now }) });
  }
  const drop = Object.entries(all).filter(([k, v]) => /^s\./.test(k)
    && (k === key ? !mine : !v || now - (v.t || 0) > SESSION_STALE_MS)).map(([k]) => k);
  if (drop.length) await chrome.storage.sync.remove(drop);
}

// What this PC's session was when last written (null: none), to write a
// new one straight away instead of on the next 30 s exchange.
let sessionShared;
const sessionKey = (s) => canon(s && s.share_session ? s.session || null : null);
const sessionChanged = (data) => !!(data.sync && data.sync.device) && sessionKey(data.sync) !== sessionShared;

// Hand buZyist what other PCs shared, then share ours. Returns the newest
// answer from buZyist (it changes when rules came from another PC).
// `doc` is there when the limits are shared, `share_session` says whether
// the running task is.
async function shareSync(data) {
  if (!data.sync || !data.sync.device) return data;
  let all;
  try {
    all = await chrome.storage.sync.get(null);
  } catch (e) {
    return data;
  }
  const now = Date.now();
  let s = data.sync;
  const limitsOn = !!s.doc;
  let remote = limitsOn ? readLimits(all) : null;
  if (remote === undefined) {
    // Two PCs wrote at once and Chrome kept chunks of both. Wait for the
    // rest to arrive; if it never does, our merged copy replaces them.
    tornSince = tornSince || now;
    if (now - tornSince > TORN_MS) remote = null;
  } else {
    tornSince = 0;
  }
  const body = {};
  if (limitsOn) {
    const others = readUsage(all, s.device, now);
    if (remote ? remote.rev !== s.rev_seen : remote === null && !s.joined) body.limits = remote;
    const seen = s.seen || {};
    if (Object.keys(others).length !== Object.keys(seen).length
        || Object.entries(others).some(([pc, u]) => seen[pc] !== u.t)) body.usage = others;
  }
  if (s.share_session) {
    const sessions = readSessions(all, s.device, now);
    const seen = s.sessions_seen || {};
    if (Object.keys(sessions).length !== Object.keys(seen).length
        || Object.entries(sessions).some(([pc, v]) => seen[pc] !== v.t)) body.sessions = sessions;
  }
  if (Object.keys(body).length) {
    const res = await post(SYNC_URL, Object.assign(body, who())).catch(() => null);
    if (!res || !res.sync) return data;
    data = res;
    s = res.sync;
  }
  try {
    await writeSession(s, all, now);
    sessionShared = sessionKey(s);
    if (s.doc) {
      if (s.joined && remote !== undefined && (!remote || canon(docOf(remote)) !== canon(docOf(s.doc)))) {
        await writeLimits(s, all);
      }
      await writeUsage(s, all, now);
    }
  } catch (e) {
    console.warn("buZyist: couldn't write to Chrome sync", e);
  }
  return data;
}

// Another PC changed something: pass it on soon (a few changes usually come together).
let syncSoon = null;
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "sync" || syncSoon) return;
  syncSoon = setTimeout(() => { syncSoon = null; sync(true); }, 2000);
});

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

function allowRule(id, priority, regexFilter) {
  return {
    id,
    priority,
    action: { type: "allow" },
    condition: { regexFilter, isUrlFilterCaseSensitive: false, resourceTypes: ["main_frame"] },
  };
}

// The declarativeNetRequest rules for this answer. Pure: the same answer (and
// the same pending time and passes) always gives the same list, with ids in
// the order of the patterns, so setRules can tell when nothing changed.
function buildRules(focus, now) {
  const addRules = [];
  const { mode, patterns, except } = rulesOf(focus);
  // The exceptions' ids follow the patterns' (fewer than LIMIT_RULE_BASE: a
  // few dozen of each at most, see MAX_BLOCKED_SITES).
  if (focus.active && mode === "allow") {
    addRules.push(redirectRule(1, CATCH_ALL_PRIORITY, "^(https?://.*)$"));
    patterns.forEach((p, i) => addRules.push(allowRule(i + 2, ALLOW_PRIORITY, patternRegex(p))));
    except.forEach((p, i) => addRules.push(redirectRule(patterns.length + 2 + i, FOCUS_PRIORITY, patternRegex(p))));
  } else if (focus.active) {
    patterns.forEach((p, i) => addRules.push(redirectRule(i + 1, FOCUS_PRIORITY, patternRegex(p))));
    except.forEach((p, i) => addRules.push(allowRule(patterns.length + 1 + i, EXCEPT_PRIORITY, patternRegex(p))));
  }
  let next = LIMIT_RULE_BASE;
  for (const rule of limitRules(focus)) {
    if (!limitState(rule, now)) continue;
    for (const p of patternsOf(rule)) {
      addRules.push(redirectRule(next++, LIMIT_PRIORITY, patternRegex(p), rule.id));
    }
  }
  return addRules;
}

// The rules last written, as canon() text. In memory only: a restarted worker
// starts from null and writes them once.
let appliedSig = null;

// Write the rules when they differ from the last ones written; true if it did.
// Most ticks change nothing, and rewriting every rule each time is costly.
async function setRules(focus, now) {
  const addRules = buildRules(focus, now);
  const sig = canon(addRules);
  if (sig === appliedSig) return false;
  const existing = await chrome.declarativeNetRequest.getDynamicRules();
  const removeRuleIds = existing.map((r) => r.id);
  try {
    await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds, addRules });
  } catch (e) {
    // One rejected regex fails the whole batch; add the rules one by one.
    console.warn("buZyist: rules rejected, adding them one at a time", e);
    await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds, addRules: [] });
    for (const rule of addRules) {
      try {
        await chrome.declarativeNetRequest.updateDynamicRules({ addRules: [rule] });
      } catch (err) {
        console.warn("buZyist: skipped rule", rule.condition.regexFilter, err);
      }
    }
  }
  // Also after a rule was skipped: retrying it every tick wouldn't help.
  appliedSig = sig;
  return true;
}

// Where a tab on this page should go instead, or null if it isn't blocked.
function blockTarget(url, focus, now) {
  if (focusBlocks(url, focus)) return BLOCKED + "?u=" + url;
  const rule = limitBlocks(url, focus, now);
  return rule ? BLOCKED + "?l=" + rule.id + "&u=" + url : null;
}

// Tabs already sitting on a site when it becomes blocked: send them over too.
// New navigations are the rules' job; this runs when the rules change.
async function sweepTabs(focus, now) {
  const tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  for (const tab of tabs) {
    if (!tab.id || !tab.url) continue;
    const target = blockTarget(tab.url, focus, now);
    if (target) chrome.tabs.update(tab.id, { url: target });
  }
}

// One tab moved to a new URL. The rules catch real navigations, but not a
// page that changes its own URL (youtube.com to youtube.com/shorts/...), so
// check it against the last answer.
function checkTab(tabId, url) {
  const focus = cache.data;
  if (!focus || !tabId || !url) return;
  const target = blockTarget(url, focus, Date.now());
  if (target) chrome.tabs.update(tabId, { url: target });
}

// 45s, 12m, 3h: short enough for the badge.
function shortTime(seconds) {
  const s = Math.max(0, Math.ceil(seconds));
  if (s < 60) return s + "s";
  const m = Math.ceil(s / 60);
  return m < 100 ? m + "m" : Math.floor(m / 60) + "h";
}

// setBadgeTextColor is Chrome 110+; older browsers keep their default.
function badgeText(color) {
  if (chrome.action.setBadgeTextColor) chrome.action.setBadgeTextColor({ color });
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
    // buZyist colours: Tomate when nearly out, Manteiga (dark text) when low, Folha otherwise.
    const low = here.left > 30 && here.left <= 120;
    chrome.action.setBadgeBackgroundColor({ color: here.left <= 30 ? "#D9361B" : low ? "#F5B83D" : "#1E6B43" });
    badgeText(low ? "#3A1410" : "#FFFFFF");
    chrome.action.setTitle({ title: `buZyist: ${shortTime(here.left)} left on ${patternsOf(here.rule).join(", ")} (${here.rule.limit_label})` });
  } else if (focus.active && focus.ends_at_ms) {
    const mins = Math.max(0, Math.round((focus.ends_at_ms - now) / 60000));
    chrome.action.setBadgeText({ text: String(mins) });
    chrome.action.setBadgeBackgroundColor({ color: "#D9361B" });
    badgeText("#FFFFFF");
    chrome.action.setTitle({ title: `buZyist: focus, ${mins} min left` });
  } else {
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "buZyist Focus" });
  }
}

// ------------------------------------------------------------------ tick

// Everything happens in one place, one at a time: credit the time since the
// last tick, exchange it with buZyist, apply the rules, work out what's being
// timed now. The popup and the block page queue up here too: two exchanges in
// flight at once would send the same pending time twice and let an older
// answer overwrite a newer one (the time left jumping up and down).
let chain = Promise.resolve();
function exclusive(fn) {
  const run = chain.then(fn);
  chain = run.catch((e) => console.warn("buZyist", e));
  return run;
}
const sync = (force) => exclusive(() => tick(force));

async function tick(force) {
  await ready;
  const started = Date.now();
  if (counting.ids.length) {
    addPending(counting.ids, Math.min((started - counting.since) / 1000, MAX_GAP_S), started);
  }
  const focus = await fetchState(force);
  const now = Date.now();
  if (await setRules(focus, now)) await sweepTabs(focus, now);
  // Counted from the start of this tick, so the time spent talking to buZyist isn't lost.
  counting = { ids: await currentIds(focus, now, await activeUrl(true)), since: started };
  updateBadge(focus, Date.now(), await activeUrl(false));
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
// Tab and window events come in bursts (onUpdated fires several times per
// page load): one exchange 250 ms after the first of them covers the lot.
let localSoon = null;
function syncSoonLocal() {
  if (localSoon) return;
  localSoon = setTimeout(() => { localSoon = null; sync(false); }, 250);
}

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.url) checkTab(tabId, info.url);
  if (info.status === "loading" || info.url) syncSoonLocal();
});
chrome.tabs.onActivated.addListener(syncSoonLocal);
chrome.tabs.onRemoved.addListener(syncSoonLocal);
chrome.windows.onFocusChanged.addListener(syncSoonLocal);
chrome.idle.setDetectionInterval(IDLE_SECONDS);
chrome.idle.onStateChanged.addListener(() => sync(false));

// ------------------------------------------------------- the block page

// blocked.js asks where things stand (so it works while buZyist is closed too),
// can grant a short pass ("allow 2 more minutes") and says when it showed a block.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  (async () => {
    await ready;
    if (msg.type === "grant") {
      const minutes = (lastGood && lastGood.pass_minutes) || 2;
      passes[msg.id] = Date.now() + minutes * 60000;
      saveState();
      try { await post(GRANT_URL, Object.assign({ id: msg.id }, who())); } catch (e) { /* the local pass still holds */ }
      await sync(true);
    }
    if (msg.type === "popup") { reply(await exclusive(() => popupData())); return; }
    if (msg.type === "blocked") {
      addBlock(msg.kind, msg.host, Date.now());
      saveState();
      reply(true);
      sync(false); // sends it
      return;
    }
    reply(await exclusive(() => blockPageData(msg)));
  })().catch((e) => { console.warn("buZyist", e); reply(null); });
  return true; // reply asynchronously
});

// What blocked.js shows: why this site is blocked and until when.
async function blockPageData(msg) {
  const focus = (await fetchState(msg.type === "grant")) || {};
  const now = Date.now();
  const rule = limitRules(focus).find((r) => r.id === msg.limit);
  const state = rule && limitState(rule, now);
  return {
    focus: {
      active: !!focus.active, task: focus.task || "", ends_at_ms: focus.ends_at_ms || 0,
      activity: focus.activity || "",
    },
    limit: rule && state ? {
      reason: state.reason,
      until: state.until,
      label: rule.limit_label,
      used_s: rule.used_s,
      budget_s: rule.budget_s,
      pattern: patternsOf(rule).join(", "),
      pass_minutes: focus.pass_minutes || 2,
    } : null,
  };
}

// What the toolbar popup shows (popup.js): the pomodoro, the limits and where
// each stands, and whether buZyist is reachable.
async function popupData() {
  const focus = (await fetchState(false)) || {};
  const now = Date.now(); // after the exchange, so it's never before counting.since
  const url = await activeUrl(false);
  const here = new Set((url ? rulesFor(focus, url) : []).map((r) => r.id));
  const upcoming = (rule) => (rule.windows_ms || []).find(([start]) => start > now);
  return {
    version: VERSION,
    offline: !!focus.offline || !lastGood,
    known: !!lastGood,
    focus: {
      active: !!focus.active, task: focus.task || "", ends_at_ms: focus.ends_at_ms || 0,
      activity: focus.activity || "", mode: focus.mode || "block", sites: (focus.patterns || []).length,
    },
    limits_on: !!focus.limits_on,
    blocks_today: blocksToday(now),
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
          const live = counting.ids.includes(rule.id)
            ? Math.max(0, Math.min((now - counting.since) / 1000, MAX_GAP_S)) : 0;
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
