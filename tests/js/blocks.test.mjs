// Block counts in the extension: addBlock, blocksToday.

import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { loadBackground } from "./load.mjs";

const plain = (value) => JSON.parse(JSON.stringify(value));
const NOW = new Date(2026, 9, 14, 15, 0, 0).getTime(); // local time, as buZyist names days
const DAY = 86400000;
let bg;

beforeEach(() => {
  bg = loadBackground();
});

test("addBlock keeps a report with an id until buZyist has it", () => {
  bg.get("addBlock")("focus", "YouTube.com", NOW);
  bg.get("addBlock")("app", "slack.exe", NOW); // not a website block
  bg.get("addBlock")("limit", "", NOW);
  const blocks = plain(bg.get("blocks"));
  assert.equal(blocks.length, 1);
  assert.match(blocks[0].id, /^[a-f0-9]{16}$/);
  assert.deepEqual({ ...blocks[0], id: "" }, { id: "", at: NOW, kind: "focus", host: "youtube.com" });
});

test("addBlock drops reports buZyist would ignore: older than 3 days, past the cap", () => {
  bg.set("blocks", [{ id: "old0000001", at: NOW - 3 * DAY - 1, kind: "focus", host: "a.com" }]);
  for (let i = 0; i < 510; i++) bg.get("addBlock")("limit", "b.com", NOW);
  const blocks = plain(bg.get("blocks"));
  assert.equal(blocks.length, 500);
  assert.ok(blocks.every((b) => b.host === "b.com"));
});

test("blocksToday adds the unsent reports to buZyist's count for today only", () => {
  const today = bg.get("localDay")(NOW);
  assert.equal(today, "2026-10-14");
  bg.set("lastGood", { blocks_today: { day: today, focus: 4, limit: 1 } });
  bg.set("blocks", [
    { id: "a000000001", at: NOW - 60000, kind: "limit", host: "a.com" },
    { id: "a000000002", at: NOW - DAY, kind: "focus", host: "a.com" }, // yesterday
  ]);
  assert.deepEqual(plain(bg.get("blocksToday")(NOW)), { focus: 4, limit: 2 });
  bg.set("lastGood", { blocks_today: { day: "2026-10-13", focus: 9, limit: 9 } }); // a day old
  assert.deepEqual(plain(bg.get("blocksToday")(NOW)), { focus: 0, limit: 1 });
});
