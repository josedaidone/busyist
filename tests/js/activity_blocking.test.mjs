// What an activity blocks, as the Apps and Websites tabs sum it up: the same
// cases as tests/test_activities.py, against ui/main.js's appBlocking and
// siteBlocking (mirrors of app_blocking and site_blocking).

import assert from "node:assert/strict";
import { test } from "node:test";

import { loadUiFunction, readJson } from "./load.mjs";

const { base, apps, sites } = readJson("tests", "activity_blocking_cases.json");
const appBlocking = loadUiFunction("appBlocking");
const siteBlocking = loadUiFunction("siteBlocking");
const PROTECTED = new Set(["chrome"]);  // the one the cases use
// JSON.parse turns the vm's objects into this realm's, for deepEqual.
const plain = (value) => JSON.parse(JSON.stringify(value));

function config(c, key) {
  const cfg = { ...base, ...(c.cfg || {}) };
  if (c.rules) cfg.activities = [{ id: "x", name: "X", icon: "dot", [key]: c.rules }];
  return cfg;
}

test("appBlocking agrees with app_blocking", () => {
  for (const c of apps) assert.deepEqual(plain(appBlocking(config(c, "apps"), "x", PROTECTED)), c.out, c.name);
});

test("siteBlocking agrees with site_blocking", () => {
  for (const c of sites) assert.deepEqual(plain(siteBlocking(config(c, "sites"), "x")), c.out, c.name);
});
