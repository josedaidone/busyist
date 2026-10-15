// Sizing a session from a task's duration: the same cases as tests/test_plan.py,
// against the UI's planSession (the examples under Settings, Todoist).

import assert from "node:assert/strict";
import { test } from "node:test";

import { loadUiFunction, readJson } from "./load.mjs";

const { defaults, cases } = readJson("tests", "plan_cases.json");
const planSession = loadUiFunction("planSession");

test("planSession agrees with plan_session", () => {
  for (const { minutes, cfg, out } of cases) {
    // JSON.parse turns the vm's arrays into this realm's, for deepEqual.
    const got = JSON.parse(JSON.stringify(planSession(minutes, { ...defaults, ...cfg })));
    assert.deepEqual(got, out, JSON.stringify({ minutes, cfg }));
  }
});
