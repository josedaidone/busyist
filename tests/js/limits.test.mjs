// Site limits in the extension: limitState, secondsLeft, addPending.

import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { loadBackground } from "./load.mjs";

// Objects made inside the worker's context have its Object.prototype; compare their JSON.
const plain = (value) => JSON.parse(JSON.stringify(value));

const NOW = Date.UTC(2026, 9, 11, 12, 0, 0);
const MINUTE = Math.floor(NOW / 60000);
let bg;
let limitState;
let secondsLeft;
let addPending;

beforeEach(() => {
  bg = loadBackground();
  limitState = bg.get("limitState");
  secondsLeft = bg.get("secondsLeft");
  addPending = bg.get("addPending");
});

const budget = (extra) => Object.assign({ id: "r1", patterns: ["youtube.com"], remaining_s: 120, limit_end_ms: NOW + 3600000 }, extra);

test("limitState: nothing set, nothing blocked", () => {
  assert.equal(limitState({ id: "r1", patterns: ["a.com"] }, NOW), null);
  assert.equal(limitState(budget(), NOW), null);
});

test("limitState: inside blocked hours", () => {
  const rule = { id: "r1", windows_ms: [[NOW - 60000, NOW + 60000], [NOW + 3600000, NOW + 7200000]] };
  assert.deepEqual(plain(limitState(rule, NOW)), { reason: "window", until: NOW + 60000 });
  assert.equal(limitState(rule, NOW + 60000), null); // the end is exclusive
  assert.equal(limitState(rule, NOW - 60001), null);
  assert.deepEqual(plain(limitState(rule, NOW + 3600000)), { reason: "window", until: NOW + 7200000 });
});

test("limitState: budget used up", () => {
  assert.deepEqual(plain(limitState(budget({ remaining_s: 0 }), NOW)), { reason: "budget", until: NOW + 3600000 });
  // The answer is about a period that has ended: it no longer blocks.
  assert.equal(limitState(budget({ remaining_s: 0 }), NOW + 3600000), null);
});

test("limitState: budget used up by time measured since the last answer", () => {
  bg.set("pending", { r1: { [MINUTE - 1]: 60, [MINUTE]: 59 } });
  assert.equal(limitState(budget(), NOW), null);
  bg.set("pending", { r1: { [MINUTE - 1]: 60, [MINUTE]: 60 } });
  assert.equal(limitState(budget(), NOW).reason, "budget");
  // Another rule's time doesn't count.
  bg.set("pending", { r2: { [MINUTE]: 600 } });
  assert.equal(limitState(budget(), NOW), null);
});

test("limitState: a pass lets the site through", () => {
  const rule = budget({ remaining_s: 0, windows_ms: [[NOW - 1, NOW + 1]] });
  assert.equal(limitState(Object.assign({}, rule, { pass_until_ms: NOW + 1000 }), NOW), null);
  assert.ok(limitState(Object.assign({}, rule, { pass_until_ms: NOW }), NOW)); // expired
  bg.set("passes", { r1: NOW + 1000 }); // granted while buZyist was out
  assert.equal(limitState(rule, NOW), null);
  assert.ok(limitState(rule, NOW + 1000));
});

test("secondsLeft", () => {
  assert.equal(secondsLeft(budget(), NOW), 120);
  assert.equal(secondsLeft({ id: "r1" }, NOW), null);
  assert.equal(secondsLeft(budget(), NOW + 3600000), null);
  bg.set("pending", { r1: { [MINUTE]: 50 } });
  assert.equal(secondsLeft(budget(), NOW), 70);
  bg.set("pending", { r1: { [MINUTE]: 60, [MINUTE - 1]: 60, [MINUTE - 2]: 60 } });
  assert.equal(secondsLeft(budget(), NOW), 0);
});

test("addPending: adds per minute, at most 60 s a minute", () => {
  addPending(["r1", "r2"], 5, NOW);
  addPending(["r1"], 30, NOW + 1000);
  assert.deepEqual(plain(bg.get("pending")), { r1: { [MINUTE]: 35 }, r2: { [MINUTE]: 5 } });
  addPending(["r1"], 40, NOW + 2000);
  assert.equal(bg.get("pending").r1[MINUTE], 60);
  addPending(["r1"], 10, NOW + 60000);
  assert.equal(bg.get("pending").r1[MINUTE + 1], 10);
});

test("addPending: drops what's older than 3 days", () => {
  const old = MINUTE - 3 * 1440 - 1;
  const edge = MINUTE - 3 * 1440;
  bg.set("pending", { r1: { [old]: 10, [edge]: 20 }, r2: { [old]: 5 } });
  addPending([], 0, NOW);
  assert.deepEqual(plain(bg.get("pending")), { r1: { [edge]: 20 }, r2: {} });
});
