// Busyist Focus - MV3 service worker.
//
// Asks Busyist (the local app) whether a work phase is running and, if so,
// redirects the blocked sites to blocked.html. Everything is driven by that
// one /focus answer: no session, a break, a pause, or Busyist being closed
// all report active:false, which removes the rules. See issue #2.

const FOCUS_URL = "http://127.0.0.1:47616/focus";
const BLOCKED = chrome.runtime.getURL("blocked.html");
const RULE_ID = 1;
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

function matches(host, sites) {
  return sites.some((s) => host === s || host.endsWith("." + s));
}

async function setRules(focus) {
  const existing = await chrome.declarativeNetRequest.getDynamicRules();
  const removeRuleIds = existing.map((r) => r.id);
  const addRules = [];
  if (focus.active && focus.sites && focus.sites.length) {
    addRules.push({
      id: RULE_ID,
      priority: 1,
      action: { type: "redirect", redirect: { regexSubstitution: BLOCKED + "?u=\\1" } },
      condition: {
        // \1 is the whole URL, handed to blocked.html so it can go back later.
        regexFilter: "^(https?://.*)$",
        requestDomains: focus.sites, // matches these domains and their subdomains
        resourceTypes: ["main_frame"],
      },
    });
  }
  await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds, addRules });
}

async function sweepTabs(focus) {
  // Tabs already sitting on a blocked site when focus starts: send them over too.
  const tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  for (const tab of tabs) {
    if (!tab.id || !tab.url) continue;
    try {
      if (matches(new URL(tab.url).hostname, focus.sites)) {
        chrome.tabs.update(tab.id, { url: BLOCKED + "?u=" + tab.url });
      }
    } catch (e) {
      /* skip tabs with an unparseable URL */
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
