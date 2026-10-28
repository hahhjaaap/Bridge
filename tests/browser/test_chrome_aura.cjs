"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const extension = path.resolve(__dirname, "../../chrome-extension/udb-aura");
const code = fs.readFileSync(path.join(extension, "background.js"), "utf8");
const tabs = new Map([[7, {id: 7, windowId: 1, url: "https://example.org/"}]]);
const state = new Map();
const groups = [];
const calls = [];
let messageListener;
let nextTabId = 8;
const chrome = {
  runtime: {
    id: "udb-test",
    getURL: file => "chrome-extension://udb-test/" + file,
    onMessage: {addListener: fn => {messageListener = fn;}}
  },
  tabs: {
    get: async id => tabs.get(id),
    create: async options => {
      calls.push(["create", options]);
      const tab = {id: nextTabId++, windowId: options.windowId || 1, url: options.url};
      tabs.set(tab.id, tab);
      return tab;
    },
    group: async options => {
      calls.push(["group", options]);
      if (options.groupId) return options.groupId;
      const group = {id: 11, windowId: 1, title: "", color: ""};
      groups.push(group);
      return group.id;
    },
    ungroup: async id => {calls.push(["ungroup", id]);},
    query: async () => [tabs.get(7)],
    onUpdated: {addListener: () => {}},
    onRemoved: {addListener: () => {}}
  },
  tabGroups: {
    query: async () => groups,
    update: async (id, update) => {
      Object.assign(groups.find(g => g.id === id), update);
    }
  },
  storage: {
    session: {
      set: async entries => {for (const [key, value] of Object.entries(entries)) state.set(key, value);},
      get: async key => ({[key]: state.get(key)}),
      remove: async key => {state.delete(key);}
    }
  },
  action: {
    setBadgeText: async arg => {calls.push(["badge", arg]);},
    setBadgeBackgroundColor: async arg => {calls.push(["badge-color", arg]);}
  },
  scripting: {
    executeScript: async arg => {calls.push(["inject", arg]);}
  }
};
const context = {chrome, URL, console};
vm.runInNewContext(code + "\nglobalThis.testApi={allowedUrl,mark,unmark};", context);
const {allowedUrl, mark, unmark} = context.testApi;

async function send(message, sender) {
  return new Promise(resolve => messageListener(message, sender, resolve));
}

(async () => {
  for (const url of [
    "file:///C:/secret", "javascript:alert(1)", "http://localhost:9222/",
    "http://127.0.0.1/", "http://192.168.1.5/", "http://10.0.0.5/",
    "http://172.17.0.1/", "http://169.254.0.1/", "http://[::1]/"
  ]) assert.equal(allowedUrl(url), false, url);
  assert.equal(allowedUrl("https://example.org"), true);
  assert.equal(allowedUrl("https://8.8.8.8"), true);

  const denied = messageListener(
    {type: "udb.launch", url: "https://example.org/"},
    {id: "other-extension", url: "chrome-extension://other/launch.html"},
    () => { throw new Error("Forged sender must not receive a response"); }
  );
  assert.equal(denied, undefined);
  assert.equal(calls.filter(c => c[0] === "create").length, 0);

  const launch = await send(
    {type: "udb.launch", url: "https://example.org/ai"},
    {id: "udb-test", url: chrome.runtime.getURL("launch.html") + "#...", tab: {windowId: 1}}
  );
  assert.equal(launch.ok, true);
  assert.equal(groups[0].title, "UDB • AI");
  assert.equal(groups[0].color, "cyan");
  assert.equal(state.get("udb:aura:" + launch.tabId), true);
  assert(calls.some(c => c[0] === "inject"));
  await unmark(launch.tabId);
  assert.equal(state.has("udb:aura:" + launch.tabId), false);
  assert(calls.some(c => c[0] === "ungroup"));
  await mark(7);
  assert.equal(state.get("udb:aura:7"), true);
  console.log("Chrome Aura background behavior: PASS");
})().catch(error => {console.error(error); process.exitCode = 1;});
