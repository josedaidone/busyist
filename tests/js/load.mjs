// Loads the extension's service worker and the UI's site-pattern check into
// Node without a build step or any change to them (node:vm, ticket #021).
// No npm packages. Run from the repo root (Node 22 takes a glob, not a folder):
//   node --test "tests/js/*.test.mjs"
//
// - loadBackground() runs extension/background.js in a fresh context with a
//   stub `chrome`. Its storage.local.get never answers, so `ready` never
//   resolves and the worker's own first sync() waits forever: nothing runs
//   unless a test calls it. Top-level `let`/`const` live in the context's
//   lexical scope, not on its global object, so read and set them with get()
//   and run().
// - loadUiFunction() pulls one top-level function out of ui/main.js (which
//   needs a whole page to run) and evaluates just that.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
export const EXTENSION_ID = "testextensionid";
export const BLOCKED = `chrome-extension://${EXTENSION_ID}/blocked.html`;

export function readJson(...parts) {
  return JSON.parse(readFileSync(join(ROOT, ...parts), "utf8"));
}

// Any property is another stub and any call returns undefined, so the
// worker's top level (addListener, alarms.create, ...) runs without Chrome.
function anything() {
  const target = function () {};
  return new Proxy(target, {
    get: (t, key) => (key in t ? t[key] : (t[key] = anything())),
    apply: () => undefined,
  });
}

function chromeStub(calls) {
  const chrome = anything();
  chrome.runtime.getManifest = () => readJson("extension", "manifest.json");
  chrome.runtime.getURL = (path) => `chrome-extension://${EXTENSION_ID}/${path}`;
  chrome.storage.local.get = () => new Promise(() => {});
  chrome.storage.local.set = (items) => {
    calls.push(["storage.local.set", items]);
    return Promise.resolve();
  };
  chrome.declarativeNetRequest.getDynamicRules = () => Promise.resolve(calls.rules.map((r) => ({ id: r.id })));
  chrome.declarativeNetRequest.updateDynamicRules = (change) => {
    calls.push(["updateDynamicRules", change]);
    const drop = new Set(change.removeRuleIds || []);
    calls.rules = calls.rules.filter((r) => !drop.has(r.id)).concat(change.addRules || []);
    return Promise.resolve();
  };
  chrome.tabs.query = () => Promise.resolve(calls.tabs);
  chrome.tabs.update = (id, props) => {
    calls.push(["tabs.update", id, props.url]);
    return Promise.resolve();
  };
  return chrome;
}

export function loadBackground() {
  const calls = [];
  calls.rules = []; // the dynamic rules as the stub holds them
  calls.tabs = []; // what chrome.tabs.query answers
  const context = vm.createContext({
    chrome: chromeStub(calls),
    console,
    crypto: globalThis.crypto,
    fetch: () => Promise.reject(new Error("no network in tests")),
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  });
  const file = join(ROOT, "extension", "background.js");
  vm.runInContext(readFileSync(file, "utf8"), context, { filename: file });
  const run = (code) => vm.runInContext(code, context);
  return {
    calls,
    run,
    get: (name) => run(name),
    // Replace one of the worker's top-level variables (pending, passes, ...).
    set: (name, value) => {
      context.__value = value;
      run(`${name} = globalThis.__value`);
      delete context.__value;
    },
  };
}

// The source of one top-level `function name(...) {...}` in a file, taken from
// its first line to the first line that is just "}" (how this code base
// formats top-level functions).
export function functionSource(path, name) {
  const lines = readFileSync(join(ROOT, path), "utf8").split(/\r?\n/);
  const start = lines.findIndex((line) => line.startsWith(`function ${name}(`));
  if (start < 0) throw new Error(`${name} not found in ${path}`);
  const end = lines.findIndex((line, i) => i > start && line === "}");
  if (end < 0) throw new Error(`end of ${name} not found in ${path}`);
  return lines.slice(start, end + 1).join("\n");
}

export function loadUiFunction(name, path = "ui/main.js") {
  const context = vm.createContext({});
  vm.runInContext(functionSource(path, name), context, { filename: path });
  return context[name];
}
