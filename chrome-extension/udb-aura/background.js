"use strict";
const PREFIX = "udb:aura:";
const GROUP_NAME = "UDB • AI";
const GROUP_COLOR = "cyan";
const tabOperations = new Map();

function serialize(tabId, operation) {
  const previous = tabOperations.get(tabId) || Promise.resolve();
  const work = previous.catch(() => {}).then(operation);
  tabOperations.set(tabId, work);
  const release = () => {
    if (tabOperations.get(tabId) === work) tabOperations.delete(tabId);
  };
  work.then(release, release);
  return work;
}

function allowedUrl(raw) {
  try {
    const url = new URL(raw);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return false;
    const host = url.hostname.toLowerCase().replace(/\.$/, "");
    if (!host || host === "localhost" || host.endsWith(".localhost")
        || host.endsWith(".local") || host.endsWith(".internal")) return false;
    if (host.startsWith("[") || host === "metadata.google.internal") return false;
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
      const [a, b] = host.split(".").map(Number);
      if (a === 0 || a === 10 || a === 127 || a >= 224
          || (a === 100 && b >= 64 && b <= 127)
          || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
          || (a === 192 && b === 168) || (a === 198 && (b === 18 || b === 19))) return false;
    }
    return true;
  } catch { return false; }
}

async function decorate(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!allowedUrl(tab.pendingUrl || tab.url || "")) return;
  try {
    await chrome.scripting.executeScript({target: {tabId}, files: ["aura.js"]});
  } catch {
    // Protected pages and browsers without host permission keep the tab group.
  }
}

function mark(tabId) {
  return serialize(tabId, () => markTab(tabId));
}

async function markTab(tabId) {
  let tab;
  // tabs.create may return before the destination URL is committed.
  for (let attempt = 0; attempt < 12; attempt++) {
    tab = await chrome.tabs.get(tabId);
    if (allowedUrl(tab.pendingUrl || tab.url || "")) break;
    if (attempt < 11) await new Promise(resolve => setTimeout(resolve, 250));
  }
  if (!allowedUrl(tab.pendingUrl || tab.url || "")) {
    throw new Error("Chỉ hỗ trợ trang web HTTP(S) công khai");
  }
  const groups = await chrome.tabGroups.query({windowId: tab.windowId});
  const existing = groups.find(group => group.title === GROUP_NAME);
  const options = {tabIds: [tabId]};
  if (existing) options.groupId = existing.id;
  else options.createProperties = {windowId: tab.windowId};
  const groupId = await chrome.tabs.group(options);
  await chrome.tabGroups.update(groupId, {title: GROUP_NAME, color: GROUP_COLOR, collapsed: false});
  await chrome.storage.session.set({[PREFIX + tabId]: true});
  await chrome.action.setBadgeText({tabId, text: "AI"});
  await chrome.action.setBadgeBackgroundColor({tabId, color: "#19B8E9"});
  await decorate(tabId);
  return {tabId, groupId};
}

function unmark(tabId) {
  return serialize(tabId, () => unmarkTab(tabId));
}

async function unmarkTab(tabId) {
  const state = await chrome.storage.session.get(PREFIX + tabId);
  if (!state[PREFIX + tabId]) return;
  await chrome.storage.session.remove(PREFIX + tabId);
  await chrome.action.setBadgeText({tabId, text: ""});
  try { await chrome.tabs.ungroup(tabId); } catch { /* tab already ungrouped */ }
  try {
    await chrome.scripting.executeScript({
      target: {tabId},
      func: () => {
        document.getElementById("__udb_aura_badge_v1")?.remove();
        document.querySelectorAll('link[data-udb-aura-favicon="1"]')
          .forEach(icon => icon.remove());
      }
    });
  } catch { /* inaccessible tab */ }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (sender.id !== chrome.runtime.id || !message || typeof message !== "object") return;
  if (message.type === "udb.launch") {
    if (!sender.url?.startsWith(chrome.runtime.getURL("launch.html")) || !allowedUrl(message.url)) {
      respond({ok: false, error: "Invalid UDB launch request"});
      return;
    }
    (async () => {
      const options = {url: message.url, active: true};
      if (sender.tab?.windowId != null) options.windowId = sender.tab.windowId;
      const tab = await chrome.tabs.create(options);
      await mark(tab.id);
      return {ok: true, tabId: tab.id};
    })().then(respond, error => respond({ok: false, error: String(error.message || error)}));
    return true;
  }
  if (message.type === "udb.mark" || message.type === "udb.unmark") {
    if (!sender.url?.startsWith(chrome.runtime.getURL("popup.html"))) {
      respond({ok: false, error: "Only the UDB popup can mark a tab"});
      return;
    }
    (async () => {
      const tabs = await chrome.tabs.query({active: true, currentWindow: true});
      if (!tabs[0]?.id) throw new Error("Không có tab đang hoạt động");
      if (message.type === "udb.mark") await mark(tabs[0].id);
      else await unmark(tabs[0].id);
      return {ok: true};
    })().then(respond, error => respond({ok: false, error: String(error.message || error)}));
    return true;
  }
});

chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status !== "complete") return;
  serialize(tabId, async () => {
    const state = await chrome.storage.session.get(PREFIX + tabId);
    if (state[PREFIX + tabId]) await decorate(tabId);
  }).catch(() => {});
});
chrome.tabs.onRemoved.addListener(tabId => {
  serialize(tabId, () => chrome.storage.session.remove(PREFIX + tabId)).catch(() => {});
});
