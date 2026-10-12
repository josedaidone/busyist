// Site patterns: the same vectors as tests/test_site_patterns.py, against the
// UI's cleanSitePattern and the extension's patternRegex/matches.

import assert from "node:assert/strict";
import { test } from "node:test";

import { loadBackground, loadUiFunction, readJson } from "./load.mjs";

const vectors = readJson("tests", "site_patterns.json");
const cleanSitePattern = loadUiFunction("cleanSitePattern");
const bg = loadBackground();
const patternRegex = bg.get("patternRegex");
const matches = bg.get("matches");

test("enough vectors", () => {
  assert.ok(vectors.clean.length >= 40);
  assert.ok(vectors.match.length >= 40);
});

test("cleanSitePattern agrees with clean_site_pattern", () => {
  for (const { in: raw, out } of vectors.clean) {
    assert.equal(cleanSitePattern(raw), out, JSON.stringify(raw));
  }
});

test("cleanSitePattern: nothing typed is not an error", () => {
  assert.equal(cleanSitePattern(""), "");
  assert.equal(cleanSitePattern("   "), "");
});

test("match patterns are in their saved form", () => {
  for (const { pattern } of vectors.match) assert.equal(cleanSitePattern(pattern), pattern, pattern);
});

test("matches", () => {
  for (const { pattern, url, hit } of vectors.match) {
    assert.equal(matches(pattern, url), hit, `${pattern} on ${url}`);
  }
});

test("matches: the compiled regex is reused and gives the same answers", () => {
  const compiled = bg.get("compiled");
  assert.equal(compiled("youtube.com"), compiled("youtube.com"));
  for (let i = 0; i < 3; i++) {
    assert.equal(matches("youtube.com", "https://m.youtube.com/"), true);
    assert.equal(matches("youtube.com", "https://notyoutube.com/"), false);
  }
});

test("patternRegex: group 1 is the whole URL", () => {
  for (const { pattern, url, hit } of vectors.match) {
    if (!hit) continue;
    const m = new RegExp(patternRegex(pattern), "i").exec(url);
    assert.equal(m[1], url, `${pattern} on ${url}`);
  }
});

// Chrome compiles declarativeNetRequest filters with RE2, Node with V8: a
// construction RE2 doesn't have would pass here and be rejected in Chrome.
function re2Problems(source) {
  const found = [];
  for (let i = 0; i < source.length; i++) {
    const c = source[i];
    if (c === "\\") {
      const next = source[i + 1] || "";
      if (/[1-9]/.test(next)) found.push("backreference \\" + next);
      if (next === "k" && source[i + 2] === "<") found.push("named backreference");
      i++; // skip the escaped character
    } else if (c === "(" && source[i + 1] === "?") {
      const head = source.slice(i, i + 4);
      if (/^\(\?(=|!|<=|<!|>)/.test(head)) found.push("lookaround or atomic group " + head);
    }
  }
  return found;
}

test("RE2: the check itself sees through escapes", () => {
  assert.deepEqual(re2Problems("a\\\\1"), []); // an escaped backslash, then 1
  assert.deepEqual(re2Problems("(a)\\1"), ["backreference \\1"]);
  assert.equal(re2Problems("(?=a)").length, 1);
  assert.equal(re2Problems("(?!a)").length, 1);
  assert.equal(re2Problems("(?<=a)").length, 1);
  assert.deepEqual(re2Problems("(?:a)"), []);
});

test("RE2: every generated filter avoids what RE2 can't compile", () => {
  const patterns = new Set([
    ...vectors.clean.filter((v) => v.out !== null).map((v) => v.out),
    ...vectors.match.map((v) => v.pattern),
  ]);
  for (const pattern of patterns) {
    const source = patternRegex(pattern);
    assert.deepEqual(re2Problems(source), [], `${pattern} -> ${source}`);
    assert.ok(source.length < 2048, pattern); // well under DNR's regex size limit
  }
  const now = Date.now();
  const focus = {
    active: true,
    mode: "allow",
    patterns: [...patterns],
    limits_on: true,
    limits: [{ id: "abc", patterns: ["youtube.com"], windows_ms: [[now - 1000, now + 1000]] }],
  };
  for (const rule of bg.get("buildRules")(focus, now)) {
    assert.deepEqual(re2Problems(rule.condition.regexFilter), [], rule.condition.regexFilter);
  }
});
