// Sharing across PCs in the extension: readLimits, canon, readUsage, readSessions.

import assert from "node:assert/strict";
import { test } from "node:test";

import { loadBackground } from "./load.mjs";

const plain = (value) => JSON.parse(JSON.stringify(value));
const bg = loadBackground();
const readLimits = bg.get("readLimits");
const canon = bg.get("canon");
const readUsage = bg.get("readUsage");
const readSessions = bg.get("readSessions");

const NOW = Date.UTC(2026, 9, 11, 12, 0, 0);
const DAY = 86400000;

test("readLimits: nothing shared yet", () => {
  assert.equal(readLimits({}), null);
  assert.equal(readLimits({ "limits.0": { rules: [] } }), null); // no rev
  assert.equal(readLimits({ "limits.0": { rev: 3, rules: [] } }), null);
});

test("readLimits: one chunk", () => {
  const all = { "limits.0": { rev: "pc.1", n: 1, on: true, on_at: 5, deleted: { x: 1 }, rules: [{ id: "a" }] } };
  assert.deepEqual(plain(readLimits(all)), { rev: "pc.1", on: true, on_at: 5, deleted: { x: 1 }, rules: [{ id: "a" }] });
});

test("readLimits: several chunks, in order", () => {
  const all = {
    "limits.0": { rev: "pc.2", n: 3, rules: [{ id: "a" }] },
    "limits.1": { rev: "pc.2", rules: [{ id: "b" }, { id: "c" }] },
    "limits.2": { rev: "pc.2", rules: [{ id: "d" }] },
  };
  const got = readLimits(all);
  assert.deepEqual(plain(got.rules.map((r) => r.id)), ["a", "b", "c", "d"]);
  assert.equal(got.on, false);
  assert.equal(got.on_at, 0);
  assert.deepEqual(plain(got.deleted), {});
});

test("readLimits: chunks from different writes are still arriving", () => {
  const torn = {
    "limits.0": { rev: "pc.3", n: 2, rules: [] },
    "limits.1": { rev: "pc.2", rules: [{ id: "b" }] },
  };
  assert.equal(readLimits(torn), undefined);
  assert.equal(readLimits({ "limits.0": { rev: "pc.3", n: 2, rules: [] } }), undefined); // one missing
  assert.equal(readLimits({ "limits.0": { rev: "pc.3", n: 1, rules: "x" } }), undefined);
});

test("canon: same content, same text, whatever the key order", () => {
  assert.equal(canon({ b: 1, a: { d: [1, { y: 2, x: 1 }], c: null } }), canon({ a: { c: null, d: [1, { x: 1, y: 2 }] }, b: 1 }));
  assert.equal(canon({ a: 1, b: 2 }), '{"a":1,"b":2}');
  assert.notEqual(canon([1, 2]), canon([2, 1])); // arrays keep their order
  assert.equal(canon(undefined), "null");
  assert.equal(canon({ a: undefined }), '{"a":null}');
  assert.equal(canon("x"), '"x"');
});

test("readUsage: other PCs' time, not this PC's or the stale", () => {
  const all = {
    "u.me.aa": { n: "ME", t: NOW, b: 1, m: {}, d: {} },
    "u.pc2.aa": { n: "Laptop", t: NOW - 1000, b: 2, m: { 1: 60 }, d: { 3: 120 } },
    "u.pc2.bb": { n: "Laptop", t: NOW - 500, b: 3 },
    "u.old.aa": { n: "Old", t: NOW - 41 * DAY, b: 4 },
    "u.pc3.zz": { n: "Bad id", t: NOW, b: 5 },
    "limits.0": { rev: "x" },
    "s.pc2": { n: "Laptop", t: NOW },
  };
  assert.deepEqual(plain(readUsage(all, "me", NOW)), {
    pc2: {
      name: "Laptop",
      t: NOW - 500,
      rules: { aa: { b: 2, m: { 1: 60 }, d: { 3: 120 } }, bb: { b: 3, m: {}, d: {} } },
    },
  });
});

test("readSessions: other PCs' running sessions, not this PC's or a day old", () => {
  const all = {
    "s.me": { n: "ME", t: NOW, stamp: 1 },
    "s.pc2": { n: "Laptop", t: NOW - 1000, stamp: 2 },
    "s.gone": { n: "Gone", t: NOW - DAY, stamp: 3 },
    "u.pc2.aa": { t: NOW },
  };
  assert.deepEqual(plain(readSessions(all, "me", NOW)), { pc2: { n: "Laptop", t: NOW - 1000, stamp: 2 } });
});
