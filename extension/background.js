// Busyist Focus - MV3 service worker.
//
// Asks Busyist (the local app) whether a work phase is running and, if so,
// redirects the blocked sites to blocked.html. In "block" mode the listed
// sites are blocked; in "allow" mode every site is, except the listed ones.
// Everything is driven by that one /focus answer: no session, a break, a
// pause, or Busyist being closed all report active:false, which removes the
// rules. See issue #2.

const FOCUS_URL = "http://127.0.0.1:47616/focus";
const BLOCKED = chrome.runtime.getURL("blocked.html");
const BLOCK_PRIORITY = 1;
const ALLOW_PRIORITY = 2; // allow rules win over the catch-all block rule
const ALARM = "busyist-sync";

let cache = { at: 0, data: null };

async function getFocus(force) {
  if (!force && cache.data && Date.now() - cache.at < 5000) return cache.data;
  let data;
  try {
    const res = await fetch(FOCUS_URL, { cache: "no-store" });
    data = res.ok ? await res.json() : { active: false, sites: [] };
  } catch (e) {
    // Busyist isn't running or can't be reached: fail open, block nothing.
    data = { active: false, sites: [] };
  }
  cache = { at: Date.now(), data };
  return data;
}

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

// Busyist versions before allowlists only send `sites` (domains to block).
function rulesOf(focus) {
  if (Array.isArray(focus.patterns)) {
    return { mode: focus.mode === "allow" ? "allow" : "block", patterns: focus.patterns };
  }
  return { mode: "block", patterns: focus.sites || [] };
}

function isBlocked(url, focus) {
  if (!/^https?:\/\//i.test(url)) return false;
  const { mode, patterns } = rulesOf(focus);
  const hit = patterns.some((p) => {
    try { return new RegExp(patternRegex(p), "i").test(url); } catch (e) { return false; }
  });
  return mode === "allow" ? !hit : hit;
}

function redirectRule(id, regexFilter) {
  return {
    id,
    priority: BLOCK_PRIORITY,
    // \1 is the whole URL, handed to blocked.html so it can go back later.
    action: { type: "redirect", redirect: { regexSubstitution: BLOCKED + "?u=\\1" } },
    condition: { regexFilter, isUrlFilterCaseSensitive: false, resourceTypes: ["main_frame"] },
  };
}

async function setRules(focus) {
  const existing = await chrome.declarativeNetRequest.getDynamicRules();
  const removeRuleIds = existing.map((r) => r.id);
  const addRules = [];
  const { mode, patterns } = rulesOf(focus);
  if (focus.active && mode === "allow") {
    addRules.push(redirectRule(1, "^(https?://.*)$"));
    patterns.forEach((p, i) => addRules.push({
      id: i + 2,
      priority: ALLOW_PRIORITY,
      action: { type: "allow" },
      condition: { regexFilter: patternRegex(p), isUrlFilterCaseSensitive: false, resourceTypes: ["main_frame"] },
    }));
  } else if (focus.active) {
    patterns.forEach((p, i) => addRules.push(redirectRule(i + 1, patternRegex(p))));
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

async function sweepTabs(focus) {
  // Tabs already sitting on a blocked site when focus starts: send them over too.
  const tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  for (const tab of tabs) {
    if (!tab.id || !tab.url) continue;
    if (isBlocked(tab.url, focus)) {
      chrome.tabs.update(tab.id, { url: BLOCKED + "?u=" + tab.url });
    }
  }
}

function updateBadge(focus) {
  if (focus.active && focus.ends_at_ms) {
    const mins = Math.max(0, Math.round((focus.ends_at_ms - Date.now()) / 60000));
    chrome.action.setBadgeText({ text: String(mins) });
    chrome.action.setBadgeBackgroundColor({ color: "#db4035" });
  } else {
    chrome.action.setBadgeText({ text: "" });
  }
}

async function sync(force) {
  const focus = await getFocus(force);
  await setRules(focus);
  updateBadge(focus);
  if (focus.active) await sweepTabs(focus);
}

function ensureAlarm() {
  // 30 s is the MV3 minimum; it also wakes the worker back up after it sleeps.
  chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
}

chrome.runtime.onInstalled.addListener(() => { ensureAlarm(); sync(true); });
chrome.runtime.onStartup.addListener(() => { ensureAlarm(); sync(true); });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) sync(true); });
chrome.tabs.onUpdated.addListener((_id, info) => { if (info.status === "loading") sync(false); });

// Also run when the worker first loads (e.g. right after "Load unpacked").
ensureAlarm();
sync(true);
