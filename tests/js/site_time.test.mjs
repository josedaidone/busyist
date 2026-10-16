// Time on every website in the extension (buZyist's "Time in apps"): hostOf, addSiteTime.

import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import { loadBackground } from "./load.mjs";

const plain = (value) => JSON.parse(JSON.stringify(value));
const NOW = new Date(2026, 9, 14, 15, 0, 30).getTime();
const MINUTE = Math.floor(NOW / 60000);
let bg;

beforeEach(() => {
  bg = loadBackground();
});

test("hostOf keeps the host of web pages only", () => {
  const hostOf = bg.get("hostOf");
  assert.equal(hostOf("https://www.YouTube.com/watch?v=1"), "youtube.com");
  assert.equal(hostOf("http://mail.google.com/"), "mail.google.com");
  assert.equal(hostOf("chrome://extensions"), "");
  assert.equal(hostOf("file:///C:/x.html"), "");
  assert.equal(hostOf(""), "");
});

test("addSiteTime adds per minute, splits focus, caps a minute at 60 s", () => {
  const add = bg.get("addSiteTime");
  add("youtube.com", 20, false, NOW);
  add("youtube.com", 30, true, NOW);
  add("youtube.com", 30, true, NOW); // only 10 s are left in this minute
  add("", 5, false, NOW);
  assert.deepEqual(plain(bg.get("siteTime")), { "youtube.com": { [MINUTE]: [60, 40] } });
});

test("addSiteTime drops what buZyist would ignore: older than 3 days", () => {
  bg.set("siteTime", { "old.com": { [MINUTE - 3 * 1440 - 1]: [10, 0] } });
  bg.get("addSiteTime")("a.com", 5, false, NOW);
  assert.deepEqual(plain(bg.get("siteTime")), { "a.com": { [MINUTE]: [5, 0] } });
});
