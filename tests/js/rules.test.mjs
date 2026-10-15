// The extension's redirect rules: buildRules, and setRules / sweepTabs /
// checkTab / saveState writing only when something changed (#016).

import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { BLOCKED, loadBackground } from "./load.mjs";

const plain = (value) => JSON.parse(JSON.stringify(value));
const NOW = Date.UTC(2026, 9, 11, 12, 0, 0);
let bg;
let buildRules;

beforeEach(() => {
  bg = loadBackground();
  buildRules = bg.get("buildRules");
});

const regexOf = (p) => bg.get("patternRegex")(p);
const summary = (rules) => plain(rules).map((r) => [r.id, r.priority, r.action.type, r.condition.regexFilter]);
const blocked = { id: "abc", patterns: ["reddit.com", "x.com"], windows_ms: [[NOW - 1000, NOW + 1000]] };
const open = { id: "def", patterns: ["news.com"], remaining_s: 60, limit_end_ms: NOW + 1000 };

test("buildRules: nothing running, nothing to block", () => {
  assert.deepEqual(plain(buildRules({ active: false, patterns: ["youtube.com"] }, NOW)), []);
  assert.deepEqual(plain(buildRules({}, NOW)), []);
});

test("buildRules: block mode", () => {
  const rules = buildRules({ active: true, mode: "block", patterns: ["youtube.com", "*.google.com"] }, NOW);
  assert.deepEqual(summary(rules), [
    [1, 4, "redirect", regexOf("youtube.com")],
    [2, 4, "redirect", regexOf("*.google.com")],
  ]);
  const rule = plain(rules[0]);
  assert.equal(rule.action.redirect.regexSubstitution, BLOCKED + "?u=\\1");
  assert.deepEqual(rule.condition.resourceTypes, ["main_frame"]);
  assert.equal(rule.condition.isUrlFilterCaseSensitive, false);
});

test("buildRules: an old buZyist that only sends sites", () => {
  const rules = buildRules({ active: true, sites: ["youtube.com"] }, NOW);
  assert.deepEqual(summary(rules), [[1, 4, "redirect", regexOf("youtube.com")]]);
});

test("buildRules: allow mode", () => {
  const rules = buildRules({ active: true, mode: "allow", patterns: ["docs.google.com", "github.com"] }, NOW);
  assert.deepEqual(summary(rules), [
    [1, 1, "redirect", "^(https?://.*)$"],
    [2, 2, "allow", regexOf("docs.google.com")],
    [3, 2, "allow", regexOf("github.com")],
  ]);
});

test("buildRules: limits that block now, above every other rule", () => {
  const focus = { active: true, mode: "block", patterns: ["youtube.com"], limits_on: true, limits: [open, blocked] };
  const rules = buildRules(focus, NOW);
  assert.deepEqual(summary(rules), [
    [1, 4, "redirect", regexOf("youtube.com")],
    [1000, 5, "redirect", regexOf("reddit.com")],
    [1001, 5, "redirect", regexOf("x.com")],
  ]);
  assert.equal(plain(rules[1]).action.redirect.regexSubstitution, BLOCKED + "?l=abc&u=\\1");
  // With the limits switched off, or out of their blocked hours, they're gone.
  assert.equal(buildRules(Object.assign({}, focus, { limits_on: false }), NOW).length, 1);
  assert.equal(buildRules(focus, NOW + 1000).length, 1);
});

test("buildRules: an activity's exceptions in block mode get through the focus blocks", () => {
  const focus = { active: true, mode: "block", patterns: ["*.google.com", "x.com"], except: ["mail.google.com"] };
  assert.deepEqual(summary(buildRules(focus, NOW)), [
    [1, 4, "redirect", regexOf("*.google.com")],
    [2, 4, "redirect", regexOf("x.com")],
    // Same priority as the redirects: Chrome prefers "allow" at a tie.
    [3, 4, "allow", regexOf("mail.google.com")],
  ]);
  // A limit still blocks an excepted site that is out of budget.
  const limited = { id: "ghi", patterns: ["mail.google.com"], windows_ms: [[NOW - 1000, NOW + 1000]] };
  const rules = buildRules(Object.assign({}, focus, { limits_on: true, limits: [limited] }), NOW);
  assert.deepEqual(summary(rules).slice(3), [[1000, 5, "redirect", regexOf("mail.google.com")]]);
});

test("buildRules: an activity's exceptions in allow mode are blocked inside the allowed sites", () => {
  const focus = { active: true, mode: "allow", patterns: ["*.google.com"], except: ["news.google.com", "play.google.com"] };
  assert.deepEqual(summary(buildRules(focus, NOW)), [
    [1, 1, "redirect", "^(https?://.*)$"],
    [2, 2, "allow", regexOf("*.google.com")],
    [3, 4, "redirect", regexOf("news.google.com")],
    [4, 4, "redirect", regexOf("play.google.com")],
  ]);
  // Without a session they're gone with the rest.
  assert.deepEqual(plain(buildRules(Object.assign({}, focus, { active: false }), NOW)), []);
});

test("focusBlocks follows the exceptions, like the rules", () => {
  const focusBlocks = bg.get("focusBlocks");
  const block = { active: true, mode: "block", patterns: ["*.google.com"], except: ["mail.google.com"] };
  assert.equal(focusBlocks("https://www.google.com/", block), true);
  assert.equal(focusBlocks("https://mail.google.com/mail/u/0", block), false);
  const allow = { active: true, mode: "allow", patterns: ["*.google.com"], except: ["news.google.com"] };
  assert.equal(focusBlocks("https://docs.google.com/", allow), false);
  assert.equal(focusBlocks("https://news.google.com/", allow), true);
  assert.equal(focusBlocks("https://example.com/", allow), true);
  // An older buZyist sends no `except`.
  assert.equal(focusBlocks("https://mail.google.com/", { active: true, mode: "block", patterns: ["*.google.com"] }), true);
});

test("buildRules: the same input gives the same rules", () => {
  const canon = bg.get("canon");
  const focus = { active: true, mode: "allow", patterns: ["a.com", "b.com"], except: ["c.a.com"], limits_on: true, limits: [blocked] };
  assert.equal(canon(buildRules(focus, NOW)), canon(buildRules(structuredClone(focus), NOW)));
});

const updates = () => bg.calls.filter(([what]) => what === "updateDynamicRules");

test("setRules: writes only when the rules change", async () => {
  const setRules = bg.get("setRules");
  const focus = { active: true, mode: "block", patterns: ["youtube.com"] };
  assert.equal(await setRules(focus, NOW), true);
  assert.equal(updates().length, 1);
  assert.equal(await setRules(structuredClone(focus), NOW + 5000), false);
  assert.equal(updates().length, 1);
  // A session ending (active:false) removes the rule that was there.
  assert.equal(await setRules({ active: false }, NOW), true);
  assert.deepEqual(plain(updates()[1][1]), { removeRuleIds: [1], addRules: [] });
  assert.equal(await setRules({ active: false }, NOW), false);
  assert.equal(updates().length, 2);
});

test("setRules: a budget that runs out changes the rules", async () => {
  const setRules = bg.get("setRules");
  const focus = { active: false, limits_on: true, limits: [open] };
  assert.equal(await setRules(focus, NOW), true); // first pass: nothing to block, still written once
  assert.equal(await setRules(focus, NOW), false);
  bg.set("pending", { def: { [Math.floor(NOW / 60000)]: 60 } });
  assert.equal(await setRules(focus, NOW), true);
  assert.deepEqual(summary(bg.calls.rules), [[1000, 5, "redirect", regexOf("news.com")]]);
});

test("setRules: a restarted worker writes the rules again", async () => {
  const focus = { active: true, mode: "block", patterns: ["youtube.com"] };
  assert.equal(await bg.get("setRules")(focus, NOW), true);
  const restarted = loadBackground();
  restarted.calls.rules = bg.calls.rules; // the rules survive in Chrome, the worker's memory doesn't
  assert.equal(await restarted.get("setRules")(focus, NOW), true);
});

test("sweepTabs and checkTab redirect tabs on blocked sites", async () => {
  const focus = { active: true, mode: "block", patterns: ["youtube.com"], limits_on: true, limits: [blocked] };
  bg.calls.tabs = [
    { id: 1, url: "https://www.youtube.com/watch?v=1" },
    { id: 2, url: "https://reddit.com/r/foo" },
    { id: 3, url: "https://example.com/" },
    { id: 0, url: "https://youtube.com/" }, // no id: skipped
  ];
  await bg.get("sweepTabs")(focus, NOW);
  const moved = () => plain(bg.calls.filter(([what]) => what === "tabs.update")).map(([, id, url]) => [id, url]);
  assert.deepEqual(moved(), [
    [1, BLOCKED + "?u=https://www.youtube.com/watch?v=1"],
    [2, BLOCKED + "?l=abc&u=https://reddit.com/r/foo"],
  ]);

  const checkTab = bg.get("checkTab");
  checkTab(7, "https://youtube.com/shorts/1"); // no answer yet: nothing to go on
  assert.equal(moved().length, 2);
  bg.set("cache", { at: NOW, data: focus });
  checkTab(7, "https://example.com/");
  checkTab(8, BLOCKED + "?u=https://youtube.com/");
  checkTab(9, "https://youtube.com/shorts/1");
  assert.deepEqual(moved().slice(2), [[9, BLOCKED + "?u=https://youtube.com/shorts/1"]]);
});

test("saveState: skips a write that changes nothing", async () => {
  const saveState = bg.get("saveState");
  const writes = () => bg.calls.filter(([what]) => what === "storage.local.set").length;
  await saveState();
  await saveState();
  assert.equal(writes(), 1);
  bg.set("pending", { abc: { 1: 5 } });
  await saveState();
  assert.equal(writes(), 2);
  bg.run("pending.abc[1] = 6"); // changed in place, as addPending does
  await saveState();
  assert.equal(writes(), 3);
  await saveState();
  assert.equal(writes(), 3);
});
